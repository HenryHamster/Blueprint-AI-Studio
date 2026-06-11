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
    _reconcile_if_not_promoted_but_pr_merged,
    _reconcile_if_pending_auto_merge_landed,
    _reconcile_if_promotion_failed_landed,
    _reconcile_if_promotion_pending_timed_out,
)
from app.time_utils import _utcnow


def _mig_source(revision: str, down_revision: str | None) -> str:
    """A minimal Alembic migration body with the given revision identifiers.

    Enough for ``build_migration_chain`` to parse the in-file ``revision`` /
    ``down_revision`` the post-merge full-chain health check reads.
    """
    down = "None" if down_revision is None else f"'{down_revision}'"
    return f"revision = '{revision}'\ndown_revision = {down}\n"


def _file_text_from_map(by_revision: dict[str, str | None]):
    """An ``app.github.get_file_text_at_ref`` mock backed by a revision map.

    ``by_revision`` maps each target-branch revision id (the ``NNNN`` filename
    prefix) to its ``down_revision``; the mock resolves a requested
    ``alembic/versions/NNNN_*.py`` path to a parseable migration source.
    """

    async def _get(path, *, token, owner, repo, ref, project_id=None):
        basename = path.rsplit("/", 1)[-1]
        prefix = basename.split("_")[0]
        if prefix not in by_revision:
            return None
        return _mig_source(prefix, by_revision[prefix])

    return _get


def _file_text_from_explicit_map(by_prefix: dict[str, tuple[str | None, str | None]]):
    """An ``app.github.get_file_text_at_ref`` mock that DECOUPLES the filename
    ``NNNN`` prefix from the in-file ``revision`` id.

    ``by_prefix`` maps each target-branch filename prefix to a
    ``(revision, down_revision)`` tuple, where ``revision`` is the in-file
    ``revision = '...'`` value (which may DIFFER from the filename prefix, or be
    ``None`` to emit a body with no parseable ``revision`` assignment at all —
    the unparseable case).  Unlike ``_file_text_from_map`` (which always keys the
    in-file revision to the filename prefix), this lets a test build the
    duplicate-in-file-revision, unparseable, and filename/revision-desync
    corruption classes the post-merge guard must detect.
    """

    async def _get(path, *, token, owner, repo, ref, project_id=None):
        basename = path.rsplit("/", 1)[-1]
        prefix = basename.split("_")[0]
        if prefix not in by_prefix:
            return None
        revision, down_revision = by_prefix[prefix]
        if revision is None:
            # Unparseable: a numeric-prefixed file with no `revision = '...'`.
            down = "None" if down_revision is None else f"'{down_revision}'"
            return f"down_revision = {down}\n"
        return _mig_source(revision, down_revision)

    return _get


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
    get_pr = mocker.patch("app.github.get_pull_request", new=AsyncMock(return_value={}))

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
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
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
        "pr_url": "https://github.com/acme/widget/pull/42",
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
    # The qualifier's composite confidence must survive into the reconcile-
    # recorded promotion so the announcement / verdict report the true score.
    assert version.last_qualification["confidence"] == 0.95


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
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
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
    # Confidence carried from the pending qualifier record into the promotion.
    assert version.last_qualification["confidence"] == 0.95


@pytest.mark.asyncio
async def test_reconcile_pending_auto_merge_calls_get_branch_sha_valid_signature(
    db_session, mocker
):
    """Regression: the reconcile path must call ``github.get_branch_sha`` with a
    signature the real function accepts.  Both reconcile sites pass
    ``project_id=...``; before ``get_branch_sha`` grew that kwarg every call
    raised ``TypeError`` (swallowed by the broad ``except`` as a warning) so the
    promotion target's HEAD was never resolved and ``promoted_sha`` silently fell
    back to ``candidate_sha``.  An ``autospec`` mock binds the REAL signature, so
    a bad call here raises just as it would in production."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    # autospec binds the real get_branch_sha signature: passing project_id is now
    # valid; the pre-fix signature (no project_id param) would raise TypeError.
    branch_sha = mocker.patch(
        "app.github.get_branch_sha", autospec=True, return_value="release0" * 5
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    candidate_sha = "abcd" * 10
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "confidence": 0.9657,
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
    # get_branch_sha was actually called (not swallowed as a TypeError) and with
    # the project_id kwarg threaded through.
    branch_sha.assert_awaited()
    assert branch_sha.await_args.kwargs.get("project_id") == str(version.project_id)
    await db_session.refresh(version)
    assert version.status == "promoted"
    # promoted_sha resolved from the promotion target HEAD, not candidate_sha.
    assert version.promoted_sha == "release0" * 5
    assert version.last_qualification["confidence"] == 0.9657


@pytest.mark.asyncio
async def test_reconcile_migration_carrying_candidate_reconciles_to_promoted(
    db_session, mocker
):
    """Regression: a migration-carrying candidate already on the target must
    reconcile to promoted, not be falsely blocked by its own just-merged
    migrations.  GitHub I/O is mocked directly; the post-merge chain validator
    (_validate_migration_chain_post_merge) is exercised for real."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    # PR touches a migration file.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0100_add_widget.py"}]
        ),
    )
    # Target branch currently has the candidate's migration (it already merged) —
    # no duplicates, so the post-merge check sees a clean chain.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0099_prev.py", "0100_add_widget.py"]),
    )
    # Full-chain health check reads each migration's source: a clean single-head
    # chain (0099 -> 0100), so the candidate's own merge is not falsely blocked.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=_file_text_from_map({"0099": None, "0100": "0099"})),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    # Test both reconcile paths (promotion_failed and pending_auto_merge).
    for build_version in [
        lambda v, sha: (
            v.__setattr__(
                "last_qualification",
                {
                    "status": "promotion_failed",
                    "candidate_sha": sha,
                    "pr_url": "https://github.com/acme/widget/pull/42",
                    "error": "merge_blocked",
                    "confidence": 0.95,
                },
            )
            or v
        ),
        lambda v, sha: (
            v.__setattr__(
                "last_qualification",
                {
                    "status": "pending",
                    "candidate_sha": sha,
                    "confidence": 0.95,
                    "promotion_pending": {
                        "pr_number": 42,
                        "pr_url": "https://github.com/acme/widget/pull/42",
                        "auto_merge_enabled": True,
                        "enabled_at": _utcnow().isoformat(),
                    },
                },
            )
            or v
        ),
    ]:
        _project, version = await _make_project_and_version(
            db_session, integration_branch="trunk", promotion_target="release"
        )
        candidate_sha = "abcd" * 10
        build_version(version, candidate_sha)
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        if version.last_qualification["status"] == "promotion_failed":
            reconciled = await _reconcile_if_promotion_failed_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )
        else:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )

        assert reconciled is True, (
            f"Expected reconciled=True for status={version.last_qualification['status']!r}"
        )
        await db_session.refresh(version)
        assert version.status == "promoted", (
            f"Expected status=promoted, got {version.status!r} "
            f"(last_qualification={version.last_qualification!r})"
        )
        assert version.last_qualification["status"] == "promoted"


@pytest.mark.asyncio
async def test_reconcile_genuine_third_party_collision_blocks(db_session, mocker):
    """Regression: a genuine third-party migration collision (two files with the
    same revision id on the target branch) must still be recorded as
    migration_guard_blocked, not silently promoted.  GitHub I/O is mocked
    directly; the post-merge chain validator is exercised for real."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    # PR touches a migration file.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0100_add_widget.py"}]
        ),
    )
    # Target branch has a duplicate revision id (0100 appears twice) — a genuine
    # third-party collision where two independent PRs both used the same slot.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=[
                "0099_prev.py",
                "0100_add_widget.py",
                "0100_add_gadget.py",  # genuine third-party duplicate
            ]
        ),
    )
    # The duplicate-id (filename-prefix) pass blocks before any source fetch, but
    # mock it anyway so an ordering change cannot turn this into a spurious defer.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=_file_text_from_map({"0099": None, "0100": "0099"})),
    )
    branch_sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    # Test both reconcile paths.
    for status, pending_data in [
        (
            "promotion_failed",
            {
                "status": "promotion_failed",
                "candidate_sha": "abcd" * 10,
                "pr_url": "https://github.com/acme/widget/pull/42",
                "error": "merge_blocked",
                "confidence": 0.95,
            },
        ),
        (
            "pending",
            {
                "status": "pending",
                "candidate_sha": "abcd" * 10,
                "confidence": 0.95,
                "promotion_pending": {
                    "pr_number": 42,
                    "pr_url": "https://github.com/acme/widget/pull/42",
                    "auto_merge_enabled": True,
                    "enabled_at": _utcnow().isoformat(),
                },
            },
        ),
    ]:
        _project, version = await _make_project_and_version(
            db_session, integration_branch="trunk", promotion_target="release"
        )
        version.last_qualification = pending_data
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        if status == "promotion_failed":
            reconciled = await _reconcile_if_promotion_failed_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )
        else:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )

        assert reconciled is True, f"Expected reconciled=True for status={status!r}"
        await db_session.refresh(version)
        assert version.status == "open", (
            f"Expected status=open (not promoted) for genuine collision, "
            f"got {version.status!r}"
        )
        assert version.last_qualification["status"] == "promotion_failed"
        assert version.last_qualification["error"] == "migration_guard_blocked"
        # The promotion was NOT recorded — no promoted_sha, no branch-sha fetch.
        branch_sha_mock.assert_not_awaited()


def _blocking_full_chain_test_params():
    """The (status, last_qualification) pairs both reconcile paths are tested with."""
    return [
        (
            "promotion_failed",
            {
                "status": "promotion_failed",
                "candidate_sha": "abcd" * 10,
                "pr_url": "https://github.com/acme/widget/pull/42",
                "error": "merge_blocked",
                "confidence": 0.95,
            },
        ),
        (
            "pending",
            {
                "status": "pending",
                "candidate_sha": "abcd" * 10,
                "confidence": 0.95,
                "promotion_pending": {
                    "pr_number": 42,
                    "pr_url": "https://github.com/acme/widget/pull/42",
                    "auto_merge_enabled": True,
                    "enabled_at": _utcnow().isoformat(),
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_reconcile_distinct_id_dual_head_blocks(db_session, mocker):
    """Regression: a genuine third-party dual head built from DISTINCT revision
    ids (no duplicate filename prefix) must block, not silently promote.  The
    filename-prefix pass cannot see this — only the full in-file chain can."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0100_add_widget.py"}]
        ),
    )
    # Distinct revision ids, every prefix unique — but 0100 and 0101 BOTH anchor
    # to 0099, so the branch has two Alembic heads (a genuine third-party fork).
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=["0099_prev.py", "0100_add_widget.py", "0101_add_gadget.py"]
        ),
    )
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(
            side_effect=_file_text_from_map(
                {"0099": None, "0100": "0099", "0101": "0099"}
            )
        ),
    )
    branch_sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    for status, pending_data in _blocking_full_chain_test_params():
        _project, version = await _make_project_and_version(
            db_session, integration_branch="trunk", promotion_target="release"
        )
        version.last_qualification = pending_data
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        if status == "promotion_failed":
            reconciled = await _reconcile_if_promotion_failed_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )
        else:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )

        assert reconciled is True, f"Expected reconciled=True for status={status!r}"
        await db_session.refresh(version)
        assert version.status == "open", (
            f"Expected status=open (not promoted) for dual head, got {version.status!r}"
        )
        assert version.last_qualification["status"] == "promotion_failed"
        assert version.last_qualification["error"] == "migration_guard_blocked"
        branch_sha_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_dangling_down_revision_blocks(db_session, mocker):
    """Regression: a migration whose down_revision points at a revision absent
    from the branch is a broken chain and must block, not silently promote."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0100_add_widget.py"}]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0099_prev.py", "0100_add_widget.py"]),
    )
    # 0100 anchors to 0098, which is not present on the branch — a broken chain.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=_file_text_from_map({"0099": None, "0100": "0098"})),
    )
    branch_sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    for status, pending_data in _blocking_full_chain_test_params():
        _project, version = await _make_project_and_version(
            db_session, integration_branch="trunk", promotion_target="release"
        )
        version.last_qualification = pending_data
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        if status == "promotion_failed":
            reconciled = await _reconcile_if_promotion_failed_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )
        else:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )

        assert reconciled is True, f"Expected reconciled=True for status={status!r}"
        await db_session.refresh(version)
        assert version.status == "open", (
            f"Expected status=open (not promoted) for broken chain, "
            f"got {version.status!r}"
        )
        assert version.last_qualification["status"] == "promotion_failed"
        assert version.last_qualification["error"] == "migration_guard_blocked"
        branch_sha_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_duplicate_in_file_revision_blocks(db_session, mocker):
    """Regression: two distinct migration files declaring the SAME in-file
    revision id (a copy-paste where the filename NNNN was bumped but the in-file
    `revision` was not) must block.  The filename-prefix pass cannot see this
    (the prefixes 0100/0101 are distinct), and chain-building COLLAPSES the two
    into a single node — which here masks what would otherwise be a dual head, so
    the un-guarded check would spuriously promote a target that fails the
    schema-revision startup gate."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0100_add_widget.py"}]
        ),
    )
    # Distinct filename prefixes (0099/0100/0101) — the filename-prefix dup pass
    # sees nothing — but 0100_add_widget and 0101_add_gadget BOTH declare in-file
    # revision '0100'.  They collapse to one node {0100: 0099}, masking the fact
    # that two files both claim the single slot after 0099.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=["0099_prev.py", "0100_add_widget.py", "0101_add_gadget.py"]
        ),
    )
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(
            side_effect=_file_text_from_explicit_map(
                {
                    "0099": ("0099", None),
                    "0100": ("0100", "0099"),
                    "0101": ("0100", "0099"),  # duplicate in-file revision
                }
            )
        ),
    )
    branch_sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    for status, pending_data in _blocking_full_chain_test_params():
        _project, version = await _make_project_and_version(
            db_session, integration_branch="trunk", promotion_target="release"
        )
        version.last_qualification = pending_data
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        if status == "promotion_failed":
            reconciled = await _reconcile_if_promotion_failed_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )
        else:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )

        assert reconciled is True, f"Expected reconciled=True for status={status!r}"
        await db_session.refresh(version)
        assert version.status == "open", (
            f"Expected status=open (not promoted) for duplicate in-file revision, "
            f"got {version.status!r}"
        )
        assert version.last_qualification["status"] == "promotion_failed"
        assert version.last_qualification["error"] == "migration_guard_blocked"
        branch_sha_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_unparseable_revision_blocks(db_session, mocker):
    """Regression: a numeric-prefixed target-branch migration whose source has no
    parseable in-file `revision` is silently DROPPED from the analyzed chain.
    Here the dropped file would otherwise be a second head (anchored at 0099), so
    dropping it masks a dual head — the guard must block on the unparseable file
    rather than promote on the truncated chain."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0100_add_widget.py"}]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=["0099_prev.py", "0100_add_widget.py", "0101_add_gadget.py"]
        ),
    )
    # 0101_add_gadget has no `revision = '...'` (revision=None) — unparseable, so
    # it is dropped from the chain.  Had it parsed (distinct id anchored at 0099)
    # it would be a second head; dropping it masks that dual head.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(
            side_effect=_file_text_from_explicit_map(
                {
                    "0099": ("0099", None),
                    "0100": ("0100", "0099"),
                    "0101": (None, "0099"),  # no in-file revision -> unparseable
                }
            )
        ),
    )
    branch_sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    for status, pending_data in _blocking_full_chain_test_params():
        _project, version = await _make_project_and_version(
            db_session, integration_branch="trunk", promotion_target="release"
        )
        version.last_qualification = pending_data
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        if status == "promotion_failed":
            reconciled = await _reconcile_if_promotion_failed_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )
        else:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )

        assert reconciled is True, f"Expected reconciled=True for status={status!r}"
        await db_session.refresh(version)
        assert version.status == "open", (
            f"Expected status=open (not promoted) for unparseable migration, "
            f"got {version.status!r}"
        )
        assert version.last_qualification["status"] == "promotion_failed"
        assert version.last_qualification["error"] == "migration_guard_blocked"
        branch_sha_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_filename_prefix_mismatch_blocks(db_session, mocker):
    """Regression: a migration whose NNNN filename prefix disagrees with its
    in-file revision id (filename 0101, in-file revision '0100') must block.  The
    in-file chain alone looks single-headed and unbroken, but the filename/revision
    desync means the file Alembic stores under 0101 actually advances the schema to
    0100 — a parity-with-pre-merge corruption class that fails the startup gate."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0101_add_gadget.py"}]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0099_prev.py", "0101_add_gadget.py"]),
    )
    # 0101_add_gadget declares in-file revision '0100' (not '0101'): a unique,
    # parseable id and a single-head chain {0099: None, 0100: 0099} — only the
    # filename/revision desync betrays the corruption.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(
            side_effect=_file_text_from_explicit_map(
                {
                    "0099": ("0099", None),
                    "0101": ("0100", "0099"),  # prefix 0101 vs in-file 0100
                }
            )
        ),
    )
    branch_sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    for status, pending_data in _blocking_full_chain_test_params():
        _project, version = await _make_project_and_version(
            db_session, integration_branch="trunk", promotion_target="release"
        )
        version.last_qualification = pending_data
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        if status == "promotion_failed":
            reconciled = await _reconcile_if_promotion_failed_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )
        else:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )

        assert reconciled is True, f"Expected reconciled=True for status={status!r}"
        await db_session.refresh(version)
        assert version.status == "open", (
            f"Expected status=open (not promoted) for filename/revision desync, "
            f"got {version.status!r}"
        )
        assert version.last_qualification["status"] == "promotion_failed"
        assert version.last_qualification["error"] == "migration_guard_blocked"
        branch_sha_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_post_merge_defers_on_source_fetch_failure(db_session, mocker):
    """Fail-safe: if a migration source cannot be fetched for the full-chain
    health check, the reconciler DEFERS (retry next sweep) rather than promoting
    on an unverified chain — mirroring the existing fail-safe semantics."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0100_add_widget.py"}]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0099_prev.py", "0100_add_widget.py"]),
    )
    # The per-file source fetch raises — migration safety is unknown, so defer.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=RuntimeError("github 502")),
    )
    branch_sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    for status, pending_data in _blocking_full_chain_test_params():
        _project, version = await _make_project_and_version(
            db_session, integration_branch="trunk", promotion_target="release"
        )
        version.last_qualification = pending_data
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        if status == "promotion_failed":
            reconciled = await _reconcile_if_promotion_failed_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )
        else:
            reconciled = await _reconcile_if_pending_auto_merge_landed(
                version.id, session_factory=_shared_session_factory(db_session)
            )

        # Defer => not reconciled this sweep, version stays open, no promotion.
        assert reconciled is False, f"Expected reconciled=False (defer) for {status!r}"
        await db_session.refresh(version)
        assert version.status == "open", (
            f"Expected status=open (deferred), got {version.status!r}"
        )
        assert version.last_qualification["status"] != "promoted"
        branch_sha_mock.assert_not_awaited()


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


# ===========================================================================
# Incident 2026-06-09: not_promoted/ci_unresolved reconciliation net.
#
# A transient DB error can roll back the promotion_pending marker write after
# a release PR is created and auto-merge is armed.  The subsequent finalize
# sweep sees no promotion_pending, re-evaluates CI as ci_unresolved (transient,
# not a hard failure), and records not_promoted — even though the PR merged
# moments later.  _reconcile_if_not_promoted_but_pr_merged catches this.
# ===========================================================================


@pytest.mark.asyncio
async def test_reconcile_not_promoted_ci_unresolved_candidate_on_main_reconciles(
    db_session, mocker
):
    """_reconcile_if_not_promoted_but_pr_merged: a version recorded as
    not_promoted/ci_unresolved whose candidate is already on main reconciles
    to promoted (regression: incident 2026-06-09 desync)."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    candidate_sha = "abcd" * 10
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
        "candidate_sha": candidate_sha,
        "confidence": 0.88,
        "pr_url": "https://github.com/acme/widget/pull/7",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_not_promoted_but_pr_merged(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    # The original confidence score must survive into the reconciled record.
    assert version.last_qualification["confidence"] == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_reconcile_not_promoted_hard_blocker_is_inert(db_session, mocker):
    """_reconcile_if_not_promoted_but_pr_merged: a hard blocker (ci_not_green)
    is NOT reconciled — hard blockers represent a genuine scoring failure, not
    a transient miss that could mask a real failure."""
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "candidate_sha": "abcd" * 10,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_not_promoted_but_pr_merged(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["blocker"] == "ci_not_green"


@pytest.mark.asyncio
async def test_reconcile_not_promoted_skips_single_branch(db_session, mocker):
    """_reconcile_if_not_promoted_but_pr_merged: on a single-branch repo (LTS
    inactive) the reconciler is inert — no GitHub I/O, version untouched."""
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="main", promotion_target="main"
    )
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
        "candidate_sha": "abcd" * 10,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_not_promoted_but_pr_merged(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"


# ===========================================================================
# Post-promotion serving confirmation.
#
# A promotion is PROVISIONAL (serving_status="unconfirmed") until deploy_health
# confirms the promoted SHA is actually serving.  These tests cover the pure
# classifier and the dedicated-session confirmation sweep across all three
# scenarios: serving-confirmed, no-new-revision deploy-failure, and a
# rollback/halt after promotion.
# ===========================================================================

from datetime import datetime  # noqa: E402

from app.models import (  # noqa: E402
    Alert,
    AlertSeverity,
    DeploymentState,
    DeploymentStatus,
    QualificationVerdict,
    ReleaseDeployArtifact,
    ReleaseVersion,
)
from app import release_artifacts as artifact_registry  # noqa: E402
from app.release_artifacts import (  # noqa: E402
    AGENT_IMAGE_ARTIFACT_KIND,
    BACKEND_REVISION_ARTIFACT_KIND,
    PROMOTION_MERGE_ARTIFACT_KIND,
    ReleaseArtifactProvider,
    _previously_deployed_sha,
    aggregate_required_artifact_status,
    artifact_is_unconfirmed,
    get_or_create_release_deploy_artifact,
    is_backend_revision_artifact,
    iter_release_artifact_providers,
)
from app.release_promotion import _record_promotion_success  # noqa: E402
from app.release_reconciliation import (  # noqa: E402
    _classify_serving_outcome,
    _confirm_serving_or_fail,
)
from app.strategic_trigger import (  # noqa: E402
    ORIGIN_QUALIFICATION,
    strategic_refresh_origin,
    strategic_refresh_pending,
)
from sqlalchemy import select as _select  # noqa: E402

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _ds(status, deployed_at, current_sha="", halted_at=None):
    """Build a transient DeploymentState for the pure classifier tests."""
    return DeploymentState(
        id=1,
        status=status,
        current_sha=current_sha,
        deployed_at=deployed_at,
        halted_at=halted_at,
    )


def test_classify_serving_confirmed_new_stable_revision():
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    now = datetime(2026, 6, 5, 12, 40, 0)
    ds = _ds(DeploymentStatus.STABLE, promoted_at.replace(minute=5), current_sha=_SHA_A)
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now)
    assert out is not None
    assert out[0] == "serving_confirmed"
    assert out[1] == _SHA_A
    assert out[2] == promoted_at.replace(minute=5)


def test_classify_sha_mismatch_is_deploy_failed():
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    now = datetime(2026, 6, 5, 12, 40, 0)
    ds = _ds(DeploymentStatus.STABLE, promoted_at.replace(minute=5), current_sha=_SHA_B)
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now)
    assert out == ("deploy_failed", "sha_mismatch", None)


def test_classify_unobservable_serving_sha_does_not_confirm_within_window():
    """A new STABLE revision booted after promotion but its live serving SHA is
    unobservable (Cloud Run lookup degraded to no value).  Within the window the
    gate must NOT confirm — and crucially must never echo the promoted SHA as a
    served_sha stand-in.  No decision yet → keep waiting."""
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    now = datetime(2026, 6, 5, 12, 40, 0)
    # STABLE, booted after the promotion, but current_sha is unobservable ("").
    ds = _ds(DeploymentStatus.STABLE, promoted_at.replace(minute=5), current_sha="")
    assert _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now) is None


def test_classify_unobservable_serving_sha_times_out_after_deadline():
    """If the serving SHA stays unobservable until the deadline elapses, the gate
    lands a DISTINCT confirmation_timed_out reason (serving_sha_unobservable) —
    NOT deploy_failed: nothing was observed going bad, the SHA just couldn't be
    confirmed in time. It never silently confirms with the promoted SHA."""
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    after = datetime(2026, 6, 5, 13, 1, 0)
    ds = _ds(DeploymentStatus.STABLE, promoted_at.replace(minute=5), current_sha="")
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, after)
    assert out == ("confirmation_timed_out", "serving_sha_unobservable", None)


def test_classify_no_new_revision_times_out_after_deadline():
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    # Old revision that predates the promotion never advanced; STABLE but stale.
    # It runs _SHA_B (the pre-promotion sha) — NOT the promoted sha (_SHA_A),
    # which is the post-merge commit that did not exist before the promotion.
    ds = _ds(
        DeploymentStatus.STABLE, datetime(2026, 6, 5, 11, 50, 0), current_sha=_SHA_B
    )
    # Within the window → no decision yet.
    assert (
        _classify_serving_outcome(
            ds, promoted_at, _SHA_A, deadline, datetime(2026, 6, 5, 12, 40, 0)
        )
        is None
    )
    # Past the deadline → confirmation_timed_out (no active failure observed).
    out = _classify_serving_outcome(
        ds, promoted_at, _SHA_A, deadline, datetime(2026, 6, 5, 13, 1, 0)
    )
    assert out == ("confirmation_timed_out", "no_new_revision", None)


def test_classify_missing_deadline_backstops_from_promoted_at():
    """A promotion with NO stamped deadline must still be bounded: the classifier
    derives the confirmation window from promoted_at so it can never wedge in an
    undecidable 'unconfirmed' forever. This is the v7 wedge regression guard."""
    from app.release_promotion import _DEPLOY_CONFIRM_WINDOW_SECONDS

    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    # Stale revision that predates the promotion never advanced; deadline=None.
    # It runs _SHA_B (the pre-promotion sha) — a pre-promotion revision cannot
    # serve the promoted sha (_SHA_A) which did not exist before the promotion.
    ds = _ds(
        DeploymentStatus.STABLE, datetime(2026, 6, 5, 11, 50, 0), current_sha=_SHA_B
    )
    # Just before the derived deadline → still waiting (no premature decision).
    within = promoted_at + timedelta(seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS - 60)
    assert _classify_serving_outcome(ds, promoted_at, _SHA_A, None, within) is None
    # Past the derived deadline → times out instead of hanging forever.
    after = promoted_at + timedelta(seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS + 60)
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, None, after)
    assert out == ("confirmation_timed_out", "no_new_revision", None)


def test_classify_rollback_after_promotion_is_deploy_failed():
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    now = datetime(2026, 6, 5, 12, 20, 0)  # within window — rollback fails immediately
    ds = _ds(DeploymentStatus.ROLLED_BACK, promoted_at.replace(minute=10))
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now)
    assert out == ("deploy_failed", "rolled_back", None)


def test_classify_halt_after_promotion_is_deploy_failed():
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    now = datetime(2026, 6, 5, 12, 20, 0)
    ds = _ds(
        DeploymentStatus.HALTED,
        promoted_at.replace(minute=2),
        halted_at=promoted_at.replace(minute=10),
    )
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now)
    assert out == ("deploy_failed", "halted", None)


def test_classify_monitoring_new_revision_waits_within_window():
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    now = datetime(2026, 6, 5, 12, 15, 0)
    # New revision booted but still MONITORING (not yet STABLE) → wait.
    ds = _ds(
        DeploymentStatus.MONITORING, promoted_at.replace(minute=5), current_sha=_SHA_A
    )
    assert _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now) is None


def test_classify_serving_confirmed_deploy_finished_before_promotion_recorded():
    """v9 race: Cloud Run revision booted BEFORE the orchestrator finished
    recording promoted_at (deployed_at < promoted_at).  An exact SHA match is
    positive evidence regardless of ordering — a pre-promotion revision cannot
    serve the promoted merge SHA, so this must confirm, not time out."""
    promoted_at = datetime(2026, 6, 5, 2, 15, 14)  # promotion finalized later
    deployed_at = datetime(2026, 6, 5, 2, 14, 54)  # deploy booted earlier (race)
    deadline = promoted_at + timedelta(hours=1)
    now = promoted_at + timedelta(minutes=5)
    ds = _ds(DeploymentStatus.STABLE, deployed_at, current_sha=_SHA_A)
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now)
    assert out is not None
    assert out[0] == "serving_confirmed"
    assert out[1] == _SHA_A
    assert out[2] == deployed_at  # served_at is the boot time


def test_classify_stable_pre_promotion_sha_mismatch_waits_not_fails():
    """A pre-promotion revision (deployed_at < promoted_at) serving a different
    SHA must NOT be reported as sha_mismatch — it predates the promotion, so
    the mismatch is expected while waiting for the new revision to boot."""
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deployed_at = datetime(2026, 6, 5, 11, 50, 0)  # predates promotion
    deadline = promoted_at + timedelta(hours=1)
    now = promoted_at + timedelta(minutes=15)
    ds = _ds(DeploymentStatus.STABLE, deployed_at, current_sha=_SHA_B)
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now)
    assert out is None  # still waiting, not sha_mismatch


async def _make_promoted_unconfirmed_version(
    db_session,
    *,
    promoted_at: datetime,
    deadline: datetime,
    promoted_sha: str = _SHA_A,
    with_verdict: bool = True,
):
    """Create a self-repo project + a v1 recorded promoted/unconfirmed."""
    project = Project(
        name="Serving-Test",
        user_id="serving-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    version.status = "promoted"
    version.promoted_at = promoted_at
    version.promoted_sha = promoted_sha
    version.serving_status = "unconfirmed"
    version.deploy_confirm_deadline = deadline
    version.last_qualification = {"status": "promoted", "promoted_sha": promoted_sha}
    db_session.add(version)
    # Commit the version BEFORE inserting the verdict so the verdict's FK to
    # releaseversion is satisfied (mirrors the real flow: the verdict row is
    # written by the promotion AFTER the version exists).
    await db_session.commit()
    await db_session.refresh(version)
    if with_verdict:
        db_session.add(
            QualificationVerdict(
                project_id=project.id,
                release_version_id=version.id,
                candidate_sha=promoted_sha,
                terminal_status="promoted",
                would_promote=True,
                promotion_enabled=True,
            )
        )
    await db_session.commit()
    db_session.add(
        ReleaseDeployArtifact(
            version_id=version.id,
            artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
            serving_status="unconfirmed",
            confirmation_deadline=deadline,
        )
    )
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


async def _set_deploy_state(db_session, **kwargs):
    existing = await db_session.get(DeploymentState, 1)
    if existing is not None:
        for k, v in kwargs.items():
            setattr(existing, k, v)
        db_session.add(existing)
    else:
        db_session.add(DeploymentState(id=1, **kwargs))
    await db_session.commit()


@pytest.fixture(autouse=True)
def _default_agent_image_gate(request, mocker, monkeypatch):
    """Default the deploy-observable serving gates so legacy tests stay green.

    Two deploy-level invariants this module's serving tests depend on:

    * **Deploy identity.** ``backend_revision`` / ``agent_image`` are now
      observable only for the project matching the deploy-level identity (which
      replaces the old ``is_self_repo`` gate). The serving tests promote an
      ``acme/widget`` project, so stamp ``DEPLOY_REPO_IDENTITY`` to match — that
      makes those Cloud Run providers observable exactly where the old
      ``is_self_repo=True`` path used to arm them.
    * **Baseline promotion_merge.** The new ``promotion_merge`` provider is
      observable for EVERY GitHub project and confirms via a live GitHub
      ancestry + check-runs lookup. Stub its provider ``check`` to confirm
      immediately so the merge-level dimension never hits the network and the
      pre-existing backend/agent serving assertions still drive the outcome.

    The agent VM image gate itself defaults to a non-bake promotion ("no
    bake-path change → confirm immediately") and the GitHub credential
    resolution + diff-base derivation are stubbed so NO test hits the network.
    Patches target the lazy-import seams ``_check_agent_image_serving`` resolves
    at call time:

      * ``app.release_promotion._resolve_github_with_session`` — the lazy import
        inside the check (the module-level binding in ``release_reconciliation``
        is a SEPARATE reference, so the promotion-reconcile paths are untouched).
      * ``app.github_refs.agent_image_rebake_expected`` — the bake-diff lookup.
      * ``app.release_artifacts._previously_deployed_sha`` — the diff base, so the
        gate gets an observable base even when the test's DeploymentState has no
        ``prior_deploy_sha``.

    Tests marked ``agent_image_gate`` opt OUT of the agent-image-specific stubs
    and drive the gate themselves so the bake / fleet / indeterminate branches
    are exercised with exactly one patch per seam (no fixture-vs-test override
    ambiguity). The deploy-identity stamp and the promotion_merge stub apply to
    ALL tests (including the opt-out ones) so the merge-level dimension never
    interferes with the agent-image scenarios.
    """
    import dataclasses

    monkeypatch.setenv("DEPLOY_REPO_IDENTITY", "acme/widget")

    async def _confirm_merge_immediately(_session, _version, _artifact, now):
        return ("serving_confirmed", getattr(_version, "promoted_sha", None), now)

    _merge_provider = artifact_registry._ARTIFACT_PROVIDERS.get(
        artifact_registry.PROMOTION_MERGE_ARTIFACT_KIND
    )
    if _merge_provider is not None:
        mocker.patch.dict(
            artifact_registry._ARTIFACT_PROVIDERS,
            {
                artifact_registry.PROMOTION_MERGE_ARTIFACT_KIND: dataclasses.replace(
                    _merge_provider, check=_confirm_merge_immediately
                )
            },
        )

    if request.node.get_closest_marker("agent_image_gate"):
        return
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.release_artifacts._previously_deployed_sha",
        return_value="p" * 40,
    )


@pytest.mark.asyncio
async def test_confirm_serving_confirmed_scenario(db_session):
    """(a) A new healthy revision boots after promotion within the window →
    serving_confirmed, served_sha stamped, verdict outcome recorded."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=40),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_A
    assert version.served_at is not None
    assert version.deploy_failure_reason is None

    state = rv.summarize_qualification_state(
        version.last_qualification,
        version.serving_status,
        version.deploy_failure_reason,
    )
    assert state["state"] == "promoted"
    assert state["serving_status"] == "serving_confirmed"

    verdict = (
        (
            await db_session.execute(
                _select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert verdict.post_promotion_outcome == "serving_confirmed"
    assert verdict.post_promotion_outcome_at is not None


@pytest.mark.asyncio
async def test_backend_artifact_row_confirms_and_projects_to_scalars(db_session):
    """Backend-only registry parity: the artifact row confirms exactly as the
    legacy scalar path did, and the scalars mirror that backend artifact row."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is True

    artifact = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind
                    == BACKEND_REVISION_ARTIFACT_KIND,
                )
            )
        )
        .scalars()
        .one()
    )
    await db_session.refresh(version)
    assert artifact.serving_status == "serving_confirmed"
    assert artifact.served_identifier == _SHA_A
    assert artifact.reason_code is None
    assert version.serving_status == artifact.serving_status
    assert version.served_sha == artifact.served_identifier
    assert version.served_sha == _SHA_A


@pytest.mark.asyncio
async def test_backend_artifact_backfill_preserves_legacy_resolved_scalars(db_session):
    """Lazy backend-row creation must inherit the stored scalar outcome instead
    of defaulting a historical promotion back to unconfirmed."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )

    artifact = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind
                    == BACKEND_REVISION_ARTIFACT_KIND,
                )
            )
        )
        .scalars()
        .one()
    )
    await db_session.delete(artifact)
    version.serving_status = "serving_confirmed"
    version.served_sha = _SHA_A
    version.served_at = promoted_at + timedelta(minutes=8)
    version.deploy_failure_reason = None
    db_session.add(version)
    await db_session.commit()

    artifact, created = await get_or_create_release_deploy_artifact(
        db_session,
        version,
        BACKEND_REVISION_ARTIFACT_KIND,
        confirmation_deadline=deadline,
    )
    await db_session.commit()

    assert created is True
    assert artifact.serving_status == "serving_confirmed"
    assert artifact.served_identifier == _SHA_A
    assert artifact.served_at == version.served_at
    assert artifact.reason_code is None


@pytest.mark.asyncio
async def test_required_artifacts_wait_for_both_and_keep_backend_projection(
    db_session, mocker
):
    """A second required provider keeps the overall outcome unresolved until it
    confirms, while the legacy scalars continue mirroring backend_revision."""
    synthetic_state = {"mode": "wait"}

    async def _check_synthetic(_session, _version, artifact, now):
        mode = synthetic_state["mode"]
        if mode == "wait":
            return None
        if mode == "confirm":
            return ("serving_confirmed", "vm-image-fingerprint", now)
        raise AssertionError(f"unexpected synthetic mode: {mode}")

    backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
        BACKEND_REVISION_ARTIFACT_KIND
    ]
    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {
            BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
            "synthetic_required": ReleaseArtifactProvider(
                artifact_kind="synthetic_required",
                check=_check_synthetic,
                required=True,
            ),
        },
        clear=True,
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is False

    artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_kind = {artifact.artifact_kind: artifact for artifact in artifacts}
    await db_session.refresh(version)
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND].serving_status == "serving_confirmed"
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND].served_identifier == _SHA_A
    assert by_kind["synthetic_required"].serving_status == "unconfirmed"
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_A
    aggregate, evidence = artifact_registry.summarize_release_artifacts(
        version, artifacts
    )
    state = rv.summarize_qualification_state(
        {"status": "promoted"},
        version.serving_status,
        version.deploy_failure_reason,
        aggregate_serving_status=aggregate["serving_status"],
        aggregate_deploy_failure_reason=aggregate["reason_code"],
        artifact_evidence=evidence,
    )
    assert aggregate["serving_status"] == "unconfirmed"
    assert state["state"] == "promoted_unconfirmed"
    assert {entry["artifact_kind"]: entry["serving_status"] for entry in evidence} == {
        BACKEND_REVISION_ARTIFACT_KIND: "serving_confirmed",
        "synthetic_required": "unconfirmed",
    }

    verdict = (
        (
            await db_session.execute(
                _select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert verdict.post_promotion_outcome is None

    synthetic_state["mode"] = "confirm"
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=30),
    )
    assert resolved is True

    await db_session.refresh(version)
    verdict = (
        (
            await db_session.execute(
                _select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert verdict.post_promotion_outcome == "serving_confirmed"
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_A
    refreshed_artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    aggregate, evidence = artifact_registry.summarize_release_artifacts(
        version, refreshed_artifacts
    )
    state = rv.summarize_qualification_state(
        {"status": "promoted"},
        version.serving_status,
        version.deploy_failure_reason,
        aggregate_serving_status=aggregate["serving_status"],
        aggregate_deploy_failure_reason=aggregate["reason_code"],
        artifact_evidence=evidence,
    )
    assert aggregate["serving_status"] == "serving_confirmed"
    assert state["state"] == "promoted"


@pytest.mark.asyncio
async def test_required_artifact_deadline_failure_records_reason_code(
    db_session, mocker
):
    """A required non-backend artifact can fail the overall outcome at its
    deadline while the backend SHA stays projected but the scalar status flips
    to deploy_failed for legacy readers."""
    synthetic_state = {"mode": "wait"}

    async def _check_synthetic(_session, _version, artifact, now):
        if synthetic_state["mode"] == "confirm":
            return ("serving_confirmed", "vm-image-fingerprint", now)
        if (
            artifact.confirmation_deadline is not None
            and now >= artifact.confirmation_deadline
        ):
            return ("deploy_failed", "synthetic_deadline_elapsed", None)
        return None

    backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
        BACKEND_REVISION_ARTIFACT_KIND
    ]
    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {
            BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
            "synthetic_required": ReleaseArtifactProvider(
                artifact_kind="synthetic_required",
                check=_check_synthetic,
                required=True,
            ),
        },
        clear=True,
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(minutes=10)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=1),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=5),
    )
    assert resolved is False

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=15),
    )
    assert resolved is True

    artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_kind = {artifact.artifact_kind: artifact for artifact in artifacts}
    await db_session.refresh(version)
    assert by_kind["synthetic_required"].serving_status == "deploy_failed"
    assert by_kind["synthetic_required"].reason_code == "synthetic_deadline_elapsed"
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND].serving_status == "serving_confirmed"
    assert version.serving_status == "deploy_failed"
    assert version.served_sha == _SHA_A
    assert version.deploy_failure_reason == "synthetic_deadline_elapsed"
    aggregate, evidence = artifact_registry.summarize_release_artifacts(
        version, artifacts
    )
    state = rv.summarize_qualification_state(
        {"status": "promoted"},
        version.serving_status,
        version.deploy_failure_reason,
        aggregate_serving_status=aggregate["serving_status"],
        aggregate_deploy_failure_reason=aggregate["reason_code"],
        artifact_evidence=evidence,
    )
    assert aggregate["serving_status"] == "deploy_failed"
    assert state["state"] == "promotion_deploy_failed"
    assert "synthetic_required" in artifact_registry.summarize_required_artifact_gap(
        evidence
    )

    verdict = (
        (
            await db_session.execute(
                _select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert verdict.post_promotion_outcome == "deploy_failed"
    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(
                    Alert.project_id == version.project_id,
                    Alert.severity == AlertSeverity.ERROR,
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        "synthetic_required" in (alert.message or "") for alert in alerts
    ), "deploy-failed alert should describe the failed required artifact"


@pytest.mark.asyncio
async def test_confirm_unobservable_sha_within_window_stays_unconfirmed(db_session):
    """A new STABLE revision booted after promotion but its serving SHA is
    unobservable: within the window the sweep must NOT confirm and must NEVER
    stamp served_sha with the promoted (merged) SHA stand-in."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # STABLE, booted after promotion, but no observable serving SHA.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha="",
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=40),
    )
    assert resolved is False
    await db_session.refresh(version)
    assert version.serving_status == "unconfirmed"
    # Critically: served_sha was NOT fabricated from the promoted SHA.
    assert version.served_sha is None


@pytest.mark.asyncio
async def test_confirm_unobservable_sha_after_deadline_times_out(db_session):
    """If the serving SHA never becomes observable before the deadline, the sweep
    lands a distinct serving_sha_unobservable CONFIRMATION_TIMED_OUT state — NOT
    deploy_failed: nothing was observed going bad, the SHA just couldn't be read.
    It never upgrades to serving_confirmed by echoing the merged SHA, and it must
    NOT fire the loud deploy-failed crisis alert."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # already elapsed
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # New STABLE revision booted after promotion, but serving SHA unobservable.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha="",
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "confirmation_timed_out"
    assert version.deploy_failure_reason == "serving_sha_unobservable"
    # No fabricated served_sha.
    assert version.served_sha is None
    # A timeout is NOT a deploy-failed crisis: no loud alert dedup stamp.
    assert version.deploy_confirm_alerted_at is None

    state = rv.summarize_qualification_state(
        version.last_qualification,
        version.serving_status,
        version.deploy_failure_reason,
    )
    assert state["state"] == "promotion_confirmation_timed_out"
    assert state["serving_status"] == "confirmation_timed_out"

    # The loud deploy-FAILED ERROR alert must NOT fire for a timeout.
    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    assert [a for a in alerts if "deploy FAILED" in a.title] == []


@pytest.mark.asyncio
async def test_confirm_no_new_revision_times_out_scenario(db_session):
    """(b) The live revision never advances within the window → CONFIRMATION
    TIMED OUT, distinct from deploy_failed: nothing was observed going bad, so NO
    loud ERROR alert fires and the verdict outcome is confirmation_timed_out. The
    never-confirmed promotion is reported as a distinct state, not cleanly
    shipped and not a crisis."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # already elapsed
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # Stale revision predating the promotion; STABLE but never advanced.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha="c" * 40,
        deployed_at=promoted_at - timedelta(minutes=30),
    )

    resolved = await _confirm_serving_or_fail(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "confirmation_timed_out"
    assert version.deploy_failure_reason == "no_new_revision"
    # No loud deploy-failed alert → no dedup stamp.
    assert version.deploy_confirm_alerted_at is None

    state = rv.summarize_qualification_state(
        version.last_qualification,
        version.serving_status,
        version.deploy_failure_reason,
    )
    # Distinct timeout-class state — NOT a clean "promoted", NOT deploy_failed.
    assert state["state"] == "promotion_confirmation_timed_out"
    assert state["serving_status"] == "confirmation_timed_out"

    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    # The loud deploy-FAILED ERROR alert is reserved for ACTIVE failures.
    assert [a for a in alerts if "deploy FAILED" in a.title] == []

    verdict = (
        (
            await db_session.execute(
                _select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert verdict.post_promotion_outcome == "confirmation_timed_out"


@pytest.mark.asyncio
async def test_confirm_rollback_after_promotion_deploy_failed_scenario(db_session):
    """(c) A rollback after promotion → deploy_failed (reason rolled_back) with
    the loud alert, even while still inside the confirmation window."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.ROLLED_BACK,
        current_sha="c" * 40,
        deployed_at=promoted_at + timedelta(minutes=10),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "deploy_failed"
    assert version.deploy_failure_reason == "rolled_back"

    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(
                    Alert.project_id == version.project_id,
                    Alert.severity == AlertSeverity.ERROR,
                )
            )
        )
        .scalars()
        .all()
    )
    assert any("deploy FAILED" in a.title for a in alerts)


@pytest.mark.asyncio
async def test_confirm_deploy_failed_arms_strategic_refresh(db_session):
    """An ACTIVE-failure (deploy_failed) resolution wakes the strategist: it arms
    exactly one coalescing strategic-refresh marker (qualification origin,
    deploy-failed reason) so recovery is not gated behind the strategic-spawn
    cooldown."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # A ROLLED_BACK after promotion is an ACTIVE failure → deploy_failed (even
    # within the window), distinct from a confirmation timeout.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.ROLLED_BACK,
        current_sha="c" * 40,
        deployed_at=promoted_at + timedelta(minutes=10),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "deploy_failed"

    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is True
    # Automated trigger (respects the failure breaker), not operator origin.
    assert strategic_refresh_origin(project.vision) == ORIGIN_QUALIFICATION
    marker = project.vision["_strategic_refresh"]
    assert str(marker.get("reason", "")).startswith("promotion_deploy_failed:")
    # Single-slot / coalescing: exactly one refresh was armed, not stacked.
    assert int(marker.get("count") or 1) == 1


@pytest.mark.asyncio
async def test_confirm_timed_out_arms_strategic_refresh_distinctly(db_session):
    """A confirmation TIMEOUT also wakes the strategist, but with a DISTINCT
    reason (promotion_confirmation_timed_out:...) — never the deploy_failed crisis
    reason — so the handoff reads as 'verify serving', not 'recover a failed
    deploy'."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # already elapsed
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # Stale revision that never advanced → no active failure → timeout.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha="c" * 40,
        deployed_at=promoted_at - timedelta(minutes=30),
    )

    resolved = await _confirm_serving_or_fail(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "confirmation_timed_out"

    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is True
    assert strategic_refresh_origin(project.vision) == ORIGIN_QUALIFICATION
    marker = project.vision["_strategic_refresh"]
    reason = str(marker.get("reason", ""))
    assert reason.startswith("promotion_confirmation_timed_out:")
    assert not reason.startswith("promotion_deploy_failed:")


@pytest.mark.asyncio
async def test_confirm_missing_promoted_sha_times_out(db_session):
    """A promoted version with NO recorded promoted_sha (corrupt record) can never
    confirm by SHA-match: the backend artifact times out but the agent_image
    provider bails on the missing SHA, so the aggregate would sit 'unconfirmed'
    forever and wedge the next version (the real v7 case that gated v8). Once the
    window elapses it must resolve to confirmation_timed_out (promoted_sha_missing),
    NOT stay unconfirmed and NOT a deploy_failed."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # already elapsed
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline, promoted_sha=None
    )
    # A STABLE revision is serving, but with no recorded promoted_sha there is
    # nothing to match it against.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "confirmation_timed_out"
    assert version.deploy_failure_reason == "promoted_sha_missing"


@pytest.mark.asyncio
async def test_confirm_missing_promoted_sha_waits_within_window(db_session):
    """The missing-promoted_sha timeout only fires AFTER the window — within it the
    version stays unconfirmed (no premature resolution)."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)  # not yet elapsed
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline, promoted_sha=None
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is False
    await db_session.refresh(version)
    assert version.serving_status == "unconfirmed"


@pytest.mark.asyncio
async def test_confirm_serving_confirmed_does_not_arm_refresh(db_session):
    """A clean serving_confirmed resolution must NOT wake the strategist —
    only failed deploys arm the refresh via this path."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=40),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"

    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is False


@pytest.mark.asyncio
async def test_confirm_within_window_unconfirmed_does_not_arm_refresh(db_session):
    """A still-in-window MONITORING no-op leaves the version unconfirmed and
    arms no refresh — the strategist is only woken on a terminal failure."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.MONITORING,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=15),
    )
    assert resolved is False
    await db_session.refresh(version)
    assert version.serving_status == "unconfirmed"

    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is False


@pytest.mark.asyncio
async def test_confirm_within_window_no_decision_keeps_unconfirmed(db_session):
    """A new revision still MONITORING within the window leaves the version
    unconfirmed (no premature confirm, no premature failure)."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.MONITORING,
        current_sha=_SHA_A,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=15),
    )
    assert resolved is False
    await db_session.refresh(version)
    assert version.serving_status == "unconfirmed"


# ===========================================================================
# Post-promotion deploy-failure RECOVERY.
#
# A serving_status="deploy_failed" latch is NOT terminal-by-construction.  A
# deploy that boot-crashed and recovered, or was merely slow past the bounded
# confirmation window (the real v5 case promoted 14:58 UTC, served 19:51 UTC),
# can later actually serve its promoted SHA.  Within a bounded recovery window
# the sweep flips it back to a truthful serving_confirmed state and clears every
# derived failure surface; beyond the window a genuinely dead deploy stays failed.
# ===========================================================================

from app.release_reconciliation import (  # noqa: E402
    _DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS_DEFAULT,
    _reconcile_if_deploy_failed_recovered,
)
from app.release_stall_alerts import (  # noqa: E402
    _deploy_failed_alert_title,
)


async def _make_promoted_deploy_failed_version(
    db_session,
    *,
    promoted_at: datetime,
    deadline: datetime,
    alerted_at: datetime,
    promoted_sha: str = _SHA_A,
    deploy_failure_reason: str = "no_new_revision",
    with_open_alert: bool = True,
):
    """Create a self-repo project + a v1 latched to promoted/deploy_failed.

    Mirrors the post-``_confirm_serving_or_fail`` deploy-failed state: the verdict
    row carries ``post_promotion_outcome="deploy_failed"`` and (optionally) a loud
    open ERROR alert with the deterministic deploy-failed title is present.
    """
    project = Project(
        name="Recover-Test",
        user_id="recover-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    version.status = "promoted"
    version.promoted_at = promoted_at
    version.promoted_sha = promoted_sha
    version.serving_status = "deploy_failed"
    version.deploy_failure_reason = deploy_failure_reason
    version.deploy_confirm_deadline = deadline
    version.deploy_confirm_alerted_at = alerted_at
    version.last_qualification = {"status": "promoted", "promoted_sha": promoted_sha}
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=promoted_sha,
            terminal_status="promoted",
            would_promote=True,
            promotion_enabled=True,
            post_promotion_outcome="deploy_failed",
            post_promotion_outcome_at=alerted_at,
        )
    )
    if with_open_alert:
        db_session.add(
            Alert(
                project_id=project.id,
                severity=AlertSeverity.ERROR,
                title=_deploy_failed_alert_title(version.version_number),
                message="deploy failed",
            )
        )
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


@pytest.mark.asyncio
async def test_recover_deploy_failed_when_sha_serves_within_window(db_session):
    """(a) A version latched deploy_failed whose promoted SHA later begins serving
    within the recovery window flips back to serving_confirmed: served_sha/at are
    stamped, deploy_failure_reason cleared, the verdict outcome flips to
    serving_confirmed, the open operator alert is dismissed, and the strategist is
    woken so the crisis directive recomputes."""
    now = _utcnow()
    promoted_at = now - timedelta(hours=5)
    deadline = promoted_at + timedelta(hours=1)  # long since elapsed
    alerted_at = promoted_at + timedelta(hours=1)  # within the 24h recovery window
    project, version = await _make_promoted_deploy_failed_version(
        db_session,
        promoted_at=promoted_at,
        deadline=deadline,
        alerted_at=alerted_at,
    )
    # A new STABLE revision finally came up healthy serving the promoted SHA.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=now - timedelta(minutes=10),
    )

    recovered = await _reconcile_if_deploy_failed_recovered(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=now,
    )
    assert recovered is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_A
    assert version.served_at is not None
    assert version.deploy_failure_reason is None

    # The view-time summary now reports the TRUE serving_confirmed state.
    state = rv.summarize_qualification_state(
        version.last_qualification,
        version.serving_status,
        version.deploy_failure_reason,
    )
    assert state["state"] == "promoted"
    assert state["serving_status"] == "serving_confirmed"

    # The verdict's post-promotion outcome flipped — the readiness precision
    # sample self-corrects off this row.
    verdict = (
        (
            await db_session.execute(
                _select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .first()
    )
    assert verdict.post_promotion_outcome == "serving_confirmed"

    # The loud deploy-failed operator alert was resolved (dismissed).
    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    deploy_alerts = [a for a in alerts if "deploy FAILED" in a.title]
    assert len(deploy_alerts) == 1
    assert deploy_alerts[0].dismissed_at is not None


@pytest.mark.asyncio
async def test_recover_confirmation_timed_out_when_sha_serves_within_window(db_session):
    """A version latched confirmation_timed_out (NOT deploy_failed) whose promoted
    SHA is later observed serving within the recovery window also recovers to
    serving_confirmed — the timeout was an observability gap, not a dead deploy."""
    now = _utcnow()
    promoted_at = now - timedelta(hours=5)
    project = Project(
        name="Recover-Timeout-Test",
        user_id="recover-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    version.status = "promoted"
    version.promoted_at = promoted_at
    version.promoted_sha = _SHA_A
    version.serving_status = "confirmation_timed_out"
    version.deploy_failure_reason = "serving_sha_unobservable"
    version.deploy_confirm_deadline = promoted_at + timedelta(hours=1)
    version.last_qualification = {"status": "promoted", "promoted_sha": _SHA_A}
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # The promoted SHA is now INDEPENDENTLY observed serving (a STABLE revision
    # booted after promotion whose live current_sha matches).
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=now - timedelta(minutes=10),
    )

    recovered = await _reconcile_if_deploy_failed_recovered(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=now,
    )
    assert recovered is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_A
    assert version.deploy_failure_reason is None

    # The strategist was woken so the "recover the failed deploy" crisis directive
    # recomputes against the now-true serving_confirmed state.
    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is True
    marker = project.vision["_strategic_refresh"]
    assert str(marker.get("reason", "")) == "promotion_deploy_recovered"


@pytest.mark.asyncio
async def test_recover_skips_when_sha_never_serves(db_session):
    """(b) A genuinely failed deploy (promoted SHA never serves — the live revision
    predates the promotion and never advanced) stays deploy_failed and keeps its
    alert and verdict outcome; no recovery, no refresh."""
    now = _utcnow()
    promoted_at = now - timedelta(hours=5)
    deadline = promoted_at + timedelta(hours=1)
    alerted_at = promoted_at + timedelta(hours=1)
    project, version = await _make_promoted_deploy_failed_version(
        db_session,
        promoted_at=promoted_at,
        deadline=deadline,
        alerted_at=alerted_at,
    )
    # Stale revision predating the promotion; STABLE but never advanced.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha="c" * 40,
        deployed_at=promoted_at - timedelta(minutes=30),
    )

    recovered = await _reconcile_if_deploy_failed_recovered(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=now,
    )
    assert recovered is False
    await db_session.refresh(version)
    assert version.serving_status == "deploy_failed"
    assert version.served_sha is None

    # The open deploy-failed alert is untouched.
    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    deploy_alerts = [a for a in alerts if "deploy FAILED" in a.title]
    assert len(deploy_alerts) == 1
    assert deploy_alerts[0].dismissed_at is None

    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is False


@pytest.mark.asyncio
async def test_recover_skips_when_non_backend_required_artifact_still_failed(
    db_session, mocker
):
    """Backend serving alone cannot recover an aggregate deploy_failed latch
    while a required non-backend artifact still reports deploy_failed."""
    backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
        BACKEND_REVISION_ARTIFACT_KIND
    ]

    async def _check_synthetic(_session, _version, _artifact, _now):
        return None

    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {
            BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
            "synthetic_required": ReleaseArtifactProvider(
                artifact_kind="synthetic_required",
                check=_check_synthetic,
                required=True,
            ),
        },
        clear=True,
    )

    now = _utcnow()
    promoted_at = now - timedelta(hours=5)
    deadline = promoted_at + timedelta(hours=1)
    alerted_at = promoted_at + timedelta(hours=1)
    project, version = await _make_promoted_deploy_failed_version(
        db_session,
        promoted_at=promoted_at,
        deadline=deadline,
        alerted_at=alerted_at,
        deploy_failure_reason="synthetic_deadline_elapsed",
    )
    db_session.add(
        ReleaseDeployArtifact(
            version_id=version.id,
            artifact_kind="synthetic_required",
            serving_status="deploy_failed",
            reason_code="synthetic_deadline_elapsed",
            confirmation_deadline=deadline,
        )
    )
    await db_session.commit()

    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=now - timedelta(minutes=10),
    )

    recovered = await _reconcile_if_deploy_failed_recovered(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=now,
    )
    assert recovered is False

    await db_session.refresh(version)
    assert version.serving_status == "deploy_failed"
    assert version.deploy_failure_reason == "synthetic_deadline_elapsed"

    alerts = (
        (
            await db_session.execute(
                _select(Alert).where(Alert.project_id == version.project_id)
            )
        )
        .scalars()
        .all()
    )
    deploy_alerts = [a for a in alerts if "deploy FAILED" in a.title]
    assert len(deploy_alerts) == 1
    assert deploy_alerts[0].dismissed_at is None

    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is False


@pytest.mark.asyncio
async def test_recover_skips_beyond_recovery_window(db_session):
    """(b) A deploy whose promoted SHA only begins serving AFTER the recovery
    window has elapsed stays deploy_failed forever — the recovery check is bounded
    so a genuinely dead deploy is no longer re-examined."""
    now = _utcnow()
    # Failure was recorded well beyond the recovery window.
    alerted_at = now - timedelta(
        seconds=_DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS_DEFAULT + 3600
    )
    promoted_at = alerted_at - timedelta(hours=1)
    deadline = alerted_at
    project, version = await _make_promoted_deploy_failed_version(
        db_session,
        promoted_at=promoted_at,
        deadline=deadline,
        alerted_at=alerted_at,
    )
    # Even though the promoted SHA is NOW genuinely serving, the window elapsed.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        deployed_at=now - timedelta(minutes=10),
    )

    recovered = await _reconcile_if_deploy_failed_recovered(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=now,
    )
    assert recovered is False
    await db_session.refresh(version)
    assert version.serving_status == "deploy_failed"
    assert version.served_sha is None


# ===========================================================================
# Agent VM image serving gate — the agent_image release deploy artifact.
#
# A self-repo promotion whose diff touches the agent VM image bake paths
# (agent/ or infra/packer/) is only fully served once a fresh agent image is
# baked AND the running fleet is observed serving the promoted source SHA.  The
# agent_image provider makes that a REQUIRED serving-verified artifact so the
# aggregate serving status (and therefore the operator panel + strategist
# payload) reflects real fleet-serving truth.  These tests pin the four
# contract behaviors, mocking GitHub I/O / fleet observation at the service
# boundary (the autouse _default_agent_image_gate fixture is re-patched here).
# ===========================================================================


async def _aggregate_serving_status(db_session, version_id):
    """Compute the operator-facing aggregate serving status from artifact rows.

    Mirrors how the version-lifecycle view / strategist payload derive truth:
    over the REQUIRED registered providers + their persisted artifact rows, not
    the legacy ``version.serving_status`` scalar (which only ever mirrors the
    backend_revision artifact)."""
    artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version_id
                )
            )
        )
        .scalars()
        .all()
    )
    providers = {p.artifact_kind: p for p in iter_release_artifact_providers()}
    rows = [(providers[a.artifact_kind], a) for a in artifacts if a.artifact_kind in providers]
    status, _reason = aggregate_required_artifact_status(rows)
    return status


def test_previously_deployed_sha_uses_prior_when_current_advanced():
    """When the post-promotion revision already advanced current_sha to the
    promoted SHA, the diff base is the recorded prior_deploy_sha."""
    ds = DeploymentState(
        id=1, current_sha=_SHA_A, prior_deploy_sha=_SHA_B, deployed_at=_utcnow()
    )
    assert _previously_deployed_sha(ds, _SHA_A) == _SHA_B


def test_previously_deployed_sha_uses_current_when_not_advanced():
    """When current_sha still holds the pre-promotion serving SHA, that is the
    diff base."""
    ds = DeploymentState(
        id=1, current_sha=_SHA_B, prior_deploy_sha="c" * 40, deployed_at=_utcnow()
    )
    assert _previously_deployed_sha(ds, _SHA_A) == _SHA_B


def test_previously_deployed_sha_indeterminate_returns_none():
    """No DeploymentState, or current==promoted with no prior, is an
    unobservable diff base → None (indeterminate)."""
    assert _previously_deployed_sha(None, _SHA_A) is None
    ds = DeploymentState(
        id=1, current_sha=_SHA_A, prior_deploy_sha=None, deployed_at=_utcnow()
    )
    assert _previously_deployed_sha(ds, _SHA_A) is None


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_bake_path_waits_for_fleet_then_confirms(db_session, mocker):
    """(a) A self-repo promotion that TOUCHES the bake paths stays unconfirmed
    until the fleet reports the promoted SHA, then flips to serving_confirmed."""
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=True),
    )
    fleet = mocker.patch(
        "app.compute.fleet_serving_source_sha",
        new=AsyncMock(return_value=False),
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # Backend revision boots serving the promoted SHA — it confirms; the agent
    # image gate alone keeps the aggregate unconfirmed.  prior_deploy_sha gives
    # the bake-diff an observable base (the SHA serving before this promotion).
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        prior_deploy_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is False
    await db_session.refresh(version)

    artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_kind = {a.artifact_kind: a for a in artifacts}
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND].serving_status == "serving_confirmed"
    assert by_kind[AGENT_IMAGE_ARTIFACT_KIND].serving_status == "unconfirmed"
    fleet.assert_awaited()
    # The operator-facing aggregate stays NOT fully serving even though the
    # backend revision booted — the agent image has not reached the fleet.
    assert await _aggregate_serving_status(db_session, version.id) == "unconfirmed"

    # The fleet now reports the promoted SHA — the agent image is live and the
    # aggregate confirms.
    fleet.return_value = True
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=30),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"

    artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_kind = {a.artifact_kind: a for a in artifacts}
    assert by_kind[AGENT_IMAGE_ARTIFACT_KIND].serving_status == "serving_confirmed"
    assert by_kind[AGENT_IMAGE_ARTIFACT_KIND].served_identifier == _SHA_A
    assert await _aggregate_serving_status(db_session, version.id) == "serving_confirmed"


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_non_bake_confirms_immediately(db_session, mocker):
    """(b) A self-repo promotion that does NOT touch the bake paths confirms the
    agent_image artifact immediately (without consulting the fleet) so the
    aggregate tracks the backend revision alone."""
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=False),
    )
    fleet = mocker.patch(
        "app.compute.fleet_serving_source_sha",
        new=AsyncMock(return_value=False),
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        prior_deploy_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"

    artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_kind = {a.artifact_kind: a for a in artifacts}
    assert by_kind[AGENT_IMAGE_ARTIFACT_KIND].serving_status == "serving_confirmed"
    assert by_kind[AGENT_IMAGE_ARTIFACT_KIND].served_identifier == _SHA_A
    # No bake expected → the fleet is never consulted.
    fleet.assert_not_awaited()


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_indeterminate_rebake_lookup_stays_unconfirmed(
    db_session, mocker
):
    """(c) An indeterminate rebake lookup (API/ref error → None) is treated as
    NOT-YET-CONFIRMED, never deploy_failed — even past the confirmation
    deadline the agent_image artifact never lands a deploy_failed of its own."""
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=None),
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        prior_deploy_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    # Within the window: agent_image cannot decide → aggregate unconfirmed.
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is False
    await db_session.refresh(version)
    assert await _aggregate_serving_status(db_session, version.id) == "unconfirmed"
    assert version.deploy_failure_reason is None

    # Past the deadline the agent_image gate STILL does not fail — indeterminate
    # signals are never escalated to deploy_failed by this provider.
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(hours=2),
    )
    assert resolved is False
    await db_session.refresh(version)
    assert await _aggregate_serving_status(db_session, version.id) == "unconfirmed"
    artifact = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind == AGENT_IMAGE_ARTIFACT_KIND,
                )
            )
        )
        .scalars()
        .one()
    )
    assert artifact.serving_status == "unconfirmed"
    assert artifact.reason_code is None


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_indeterminate_no_fleet_reported_stays_unconfirmed(
    db_session, mocker
):
    """(c) A bake IS expected but no fleet VM has reported a source SHA yet —
    the fleet observation is indeterminate → not-yet-confirmed, never
    deploy_failed."""
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.compute.fleet_serving_source_sha",
        new=AsyncMock(return_value=False),
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        prior_deploy_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(hours=2),  # past the deadline
    )
    assert resolved is False
    await db_session.refresh(version)
    # Aggregate stays NOT fully serving (never deploy_failed) and the agent_image
    # artifact carries no failure reason — indeterminate fleet ≠ failed deploy.
    assert await _aggregate_serving_status(db_session, version.id) == "unconfirmed"
    assert version.deploy_failure_reason is None
    artifact = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind == AGENT_IMAGE_ARTIFACT_KIND,
                )
            )
        )
        .scalars()
        .one()
    )
    assert artifact.serving_status == "unconfirmed"
    assert artifact.reason_code is None


@pytest.mark.asyncio
async def test_self_repo_promotion_creates_both_serving_artifacts(db_session):
    """A deploy-identity promotion opens the multi-artifact serving window: the
    baseline promotion_merge AND both Cloud Run artifacts (backend_revision +
    agent_image) are created with the confirmation deadline, alongside an
    unconfirmed version serving status. The autouse fixture stamps
    DEPLOY_REPO_IDENTITY=acme/widget so the deploy-level providers are observable
    for this project — replacing the old is_self_repo arming gate."""
    project = Project(
        name="Promote-SelfRepo",
        user_id="promote-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
        is_self_repo=True,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _record_promotion_success(
        db_session,
        version,
        "cand" * 10,
        {"status": "promoted", "confidence": 0.9},
        "https://github.com/acme/widget/pull/7",
        promoted_sha=_SHA_A,
        lock_released=False,
    )
    await db_session.commit()
    assert result["status"] == "promoted"

    await db_session.refresh(version)
    assert version.serving_status == "unconfirmed"
    assert version.deploy_confirm_deadline is not None

    artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    kinds = {a.artifact_kind for a in artifacts}
    assert kinds == {
        PROMOTION_MERGE_ARTIFACT_KIND,
        BACKEND_REVISION_ARTIFACT_KIND,
        AGENT_IMAGE_ARTIFACT_KIND,
    }
    for a in artifacts:
        assert a.confirmation_deadline == version.deploy_confirm_deadline


@pytest.mark.asyncio
async def test_non_deploy_github_promotion_arms_merge_only(db_session):
    """(d) An EXTERNAL GitHub project (not the deploy identity) is now armed at
    the merge level: only the baseline ``promotion_merge`` artifact is created —
    NOT the Cloud Run backend_revision / agent_image, which are observable only
    for the deploy's own repo. serving_status flips to ``unconfirmed`` so the
    pipeline can answer "did the last version ship?" for ANY promoted project,
    replacing the old is_self_repo-only arming.

    The autouse fixture stamps DEPLOY_REPO_IDENTITY=acme/widget; this project is
    ``external/repo``, so it does NOT match the deploy identity."""
    project = Project(
        name="Reconcile-External",
        user_id="reconcile-user",
        github_owner="external",
        github_repo="repo",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    result = await _record_promotion_success(
        db_session,
        version,
        "cand" * 10,
        {"status": "promoted", "confidence": 0.9},
        "https://github.com/external/repo/pull/9",
        promoted_sha=_SHA_A,
        lock_released=False,
    )
    await db_session.commit()
    assert result["status"] == "promoted"

    await db_session.refresh(version)
    # Armed at the merge level (no longer NULL/NA for an external project).
    assert version.serving_status == "unconfirmed"
    assert version.deploy_confirm_deadline is not None

    artifacts = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    # Only the baseline merge-level provider — the Cloud Run providers are
    # unobservable for a non-deploy project, so they get no row.
    assert {a.artifact_kind for a in artifacts} == {PROMOTION_MERGE_ARTIFACT_KIND}


def test_summarize_skips_absent_required_non_backend_provider():
    """A promoted version with a CONFIRMED backend_revision row and NO
    agent_image row must NOT be stranded ``unconfirmed`` by the absent required
    non-backend provider.

    This guards the ``summarize_release_artifacts`` missing-required-non-backend
    -row branch directly: the absent provider contributes a vacuous (``None``)
    evidence row that is excluded from the aggregate, so the aggregate is
    computed over the rows that actually exist (here, the confirmed
    backend_revision alone) and resolves to ``serving_confirmed`` — not the
    false-negative ``unconfirmed`` the prior behavior produced. Operator-facing
    serving truth flows from this aggregate into admin_release_version,
    strategic_spawning, and release_reconciliation, so a regression here would
    silently flip every such promoted version to a permanent false-negative.
    """
    # Precondition: the agent_image provider must be registered REQUIRED, else
    # this test would not exercise the absent-required-non-backend-row branch.
    agent_provider = artifact_registry._ARTIFACT_PROVIDERS[AGENT_IMAGE_ARTIFACT_KIND]
    assert agent_provider.required is True
    assert not is_backend_revision_artifact(AGENT_IMAGE_ARTIFACT_KIND)

    version = ReleaseVersion(
        project_id=1,
        version_number=1,
        status="promoted",
        promoted_sha=_SHA_A,
        served_sha=_SHA_A,
        serving_status="serving_confirmed",
    )
    backend_row = ReleaseDeployArtifact(
        version_id=1,
        artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
        serving_status="serving_confirmed",
        served_identifier=_SHA_A,
    )

    aggregate, evidence = artifact_registry.summarize_release_artifacts(
        version, [backend_row]
    )

    # The aggregate is NOT stranded unconfirmed by the absent agent_image row.
    assert aggregate["serving_status"] == "serving_confirmed"
    assert aggregate["reason_code"] is None

    by_kind = {entry["artifact_kind"]: entry for entry in evidence}
    # The absent required provider is surfaced as a vacuous (None) evidence row
    # rather than being silently dropped — it just does not feed the aggregate.
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND]["serving_status"] == (
        "serving_confirmed"
    )
    assert by_kind[AGENT_IMAGE_ARTIFACT_KIND]["serving_status"] is None
    assert by_kind[AGENT_IMAGE_ARTIFACT_KIND]["required"] is True


def test_summarize_unarmed_promotion_is_na_not_phantom_unconfirmed():
    """A promoted version that was NEVER armed for serving confirmation must NOT
    be projected ``unconfirmed`` — it reads as plain ``promoted`` (NA).

    The real v3..v7 wedge: serving-confirmation arming (scalar
    ``serving_status="unconfirmed"`` + the backend/agent artifact ROWS) only
    runs for promotions whose deploy is observable.  An un-armed promotion has
    ``serving_status`` NULL and NO artifact rows.  The prior projection
    synthesized ``unconfirmed`` from ``status=="promoted"`` alone, manufacturing
    a pending serving state with NO resolution path: the serving-confirmation
    sweep only selects versions whose scalar is ``"unconfirmed"`` OR that have a
    real pending artifact row, so an un-armed promotion sat falsely
    ``unconfirmed`` forever and the strategist held the next version on it.

    With the fix the scalar is projected as-is (NULL -> NA), so the aggregate is
    NA and ``summarize_qualification_state`` returns the truthful plain
    ``promoted`` — the version shipped; there is simply no serving signal being
    tracked for it.
    """
    version = ReleaseVersion(
        project_id=1,
        version_number=7,
        status="promoted",
        # The corruption shape: un-armed promotion — no scalar, no rows.
        serving_status=None,
        promoted_sha=None,
    )

    # No artifact rows exist at all (the un-armed case).
    aggregate, evidence = artifact_registry.summarize_release_artifacts(version, [])

    # The aggregate is NA — NOT a phantom "unconfirmed" that nothing can resolve.
    assert aggregate["serving_status"] is None
    assert aggregate["reason_code"] is None

    by_kind = {entry["artifact_kind"]: entry for entry in evidence}
    # The backend_revision projection no longer manufactures "unconfirmed" from
    # status=="promoted"; it projects the (NULL) scalar directly.
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND]["serving_status"] is None
    assert by_kind[AGENT_IMAGE_ARTIFACT_KIND]["serving_status"] is None

    # End-to-end: the qualification state is the truthful plain "promoted",
    # NOT "promoted_unconfirmed" (which would wedge the next version).
    state = rv.summarize_qualification_state(
        {"status": "promoted"},
        version.serving_status,
        version.deploy_failure_reason,
        aggregate_serving_status=aggregate["serving_status"],
        aggregate_deploy_failure_reason=aggregate["reason_code"],
        artifact_evidence=evidence,
    )
    assert state["state"] == "promoted"


def test_summarize_armed_unconfirmed_promotion_still_gates():
    """The fix must NOT weaken the legitimate pending case: an ARMED promotion
    (scalar ``unconfirmed``, no row yet) still projects ``unconfirmed`` so a
    real in-window deploy continues to gate.
    """
    version = ReleaseVersion(
        project_id=1,
        version_number=8,
        status="promoted",
        serving_status="unconfirmed",
        promoted_sha=_SHA_A,
    )

    aggregate, evidence = artifact_registry.summarize_release_artifacts(version, [])

    assert aggregate["serving_status"] == "unconfirmed"
    by_kind = {entry["artifact_kind"]: entry for entry in evidence}
    assert by_kind[BACKEND_REVISION_ARTIFACT_KIND]["serving_status"] == "unconfirmed"


# ===========================================================================
# Unobservable-provider orphan resolution (Bug: split-brain pending rows).
#
# When a REQUIRED artifact row was armed when the deploy identity matched but
# the provider is now unobservable (identity changed / cleared), the row must
# be resolved rather than orphaned.  Past its deadline it must be timed out
# with reason_code="provider_unobservable"; within the deadline it counts as
# still pending so the sweep keeps trying (identity may be restored).
# ===========================================================================


@pytest.mark.asyncio
async def test_confirm_unobservable_required_row_resolves_past_deadline(
    db_session, monkeypatch
):
    """An armed REQUIRED artifact row whose provider is now unobservable (deploy
    identity cleared after arming) must NOT orphan forever.  Past its
    confirmation_deadline it must be timed out with reason_code=
    'provider_unobservable' so the aggregate converges and the version exits
    'unconfirmed'."""
    # Clear deploy identity: backend_revision + agent_image become unobservable.
    monkeypatch.delenv("DEPLOY_REPO_IDENTITY", raising=False)

    promoted_at = _utcnow() - timedelta(hours=3)
    deadline = _utcnow() - timedelta(hours=1)  # past deadline
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # _make_promoted_unconfirmed_version creates the backend_revision row;
    # with deploy identity cleared this provider is now unobservable.

    resolved = await _confirm_serving_or_fail(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert resolved is True, "must transition out of unconfirmed"

    await db_session.refresh(version)
    # The version must exit 'unconfirmed'; confirmation_timed_out is expected
    # (backend_revision past deadline → provider_unobservable; promotion_merge
    # was stubbed to confirm by the autouse fixture).
    assert version.serving_status not in (
        "unconfirmed",
    ), f"version still unconfirmed: {version.serving_status}"

    # The backend_revision artifact row must be resolved.
    artifact = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind == BACKEND_REVISION_ARTIFACT_KIND,
                )
            )
        )
        .scalars()
        .first()
    )
    assert artifact is not None
    assert artifact.serving_status == "confirmation_timed_out"
    assert artifact.reason_code == "provider_unobservable"


@pytest.mark.asyncio
async def test_confirm_unobservable_required_row_stays_pending_within_deadline(
    db_session, monkeypatch
):
    """Within the confirmation window, an unobservable required artifact row must
    still count as pending (had_required_unconfirmed) so the sweep keeps
    retrying — the deploy identity may be restored before the deadline."""
    monkeypatch.delenv("DEPLOY_REPO_IDENTITY", raising=False)

    promoted_at = _utcnow() - timedelta(minutes=5)
    deadline = _utcnow() + timedelta(hours=2)  # still within window
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )

    resolved = await _confirm_serving_or_fail(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    # Within deadline: no transition yet (still waiting for identity to come back).
    assert resolved is False

    await db_session.refresh(version)
    assert version.serving_status == "unconfirmed"

    # The backend_revision artifact row must remain unconfirmed (not orphaned).
    artifact = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind == BACKEND_REVISION_ARTIFACT_KIND,
                )
            )
        )
        .scalars()
        .first()
    )
    assert artifact is not None
    assert artifact_is_unconfirmed(artifact)
