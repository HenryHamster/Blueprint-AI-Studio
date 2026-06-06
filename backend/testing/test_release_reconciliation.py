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
            return_value=[
                {"filename": "backend/alembic/versions/0100_add_widget.py"}
            ]
        ),
    )
    # Target branch currently has the candidate's migration (it already merged) —
    # no duplicates, so the post-merge check sees a clean chain.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=["0099_prev.py", "0100_add_widget.py"]
        ),
    )
    # Full-chain health check reads each migration's source: a clean single-head
    # chain (0099 -> 0100), so the candidate's own merge is not falsely blocked.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(
            side_effect=_file_text_from_map({"0099": None, "0100": "0099"})
        ),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="release0" * 5),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    # Test both reconcile paths (promotion_failed and pending_auto_merge).
    for build_version in [
        lambda v, sha: v.__setattr__(
            "last_qualification",
            {
                "status": "promotion_failed",
                "candidate_sha": sha,
                "pr_url": "https://github.com/acme/widget/pull/42",
                "error": "merge_blocked",
                "confidence": 0.95,
            },
        )
        or v,
        lambda v, sha: v.__setattr__(
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
        or v,
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
            return_value=[
                {"filename": "backend/alembic/versions/0100_add_widget.py"}
            ]
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
        new=AsyncMock(
            side_effect=_file_text_from_map({"0099": None, "0100": "0099"})
        ),
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
            return_value=[
                {"filename": "backend/alembic/versions/0100_add_widget.py"}
            ]
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
            f"Expected status=open (not promoted) for dual head, "
            f"got {version.status!r}"
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
            return_value=[
                {"filename": "backend/alembic/versions/0100_add_widget.py"}
            ]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=["0099_prev.py", "0100_add_widget.py"]
        ),
    )
    # 0100 anchors to 0098, which is not present on the branch — a broken chain.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(
            side_effect=_file_text_from_map({"0099": None, "0100": "0098"})
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
            return_value=[
                {"filename": "backend/alembic/versions/0100_add_widget.py"}
            ]
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
            return_value=[
                {"filename": "backend/alembic/versions/0100_add_widget.py"}
            ]
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
            return_value=[
                {"filename": "backend/alembic/versions/0101_add_gadget.py"}
            ]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=["0099_prev.py", "0101_add_gadget.py"]
        ),
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
            return_value=[
                {"filename": "backend/alembic/versions/0100_add_widget.py"}
            ]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=["0099_prev.py", "0100_add_widget.py"]
        ),
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
)
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


def test_classify_no_new_revision_only_after_deadline():
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    # Old revision that predates the promotion never advanced; STABLE but stale.
    ds = _ds(DeploymentStatus.STABLE, datetime(2026, 6, 5, 11, 50, 0), current_sha=_SHA_A)
    # Within the window → no decision yet.
    assert (
        _classify_serving_outcome(
            ds, promoted_at, _SHA_A, deadline, datetime(2026, 6, 5, 12, 40, 0)
        )
        is None
    )
    # Past the deadline → deploy_failed.
    out = _classify_serving_outcome(
        ds, promoted_at, _SHA_A, deadline, datetime(2026, 6, 5, 13, 1, 0)
    )
    assert out == ("deploy_failed", "no_new_revision", None)


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
    ds = _ds(DeploymentStatus.MONITORING, promoted_at.replace(minute=5), current_sha=_SHA_A)
    assert _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now) is None


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
        version.last_qualification, version.serving_status, version.deploy_failure_reason
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
async def test_confirm_no_new_revision_deploy_failed_scenario(db_session):
    """(b) The live revision never advances within the window → deploy_failed,
    a loud ERROR alert fires, a distinct state is reported, and the never-shipped
    promotion is NOT summarized as cleanly shipped."""
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
    assert version.serving_status == "deploy_failed"
    assert version.deploy_failure_reason == "no_new_revision"
    assert version.deploy_confirm_alerted_at is not None

    state = rv.summarize_qualification_state(
        version.last_qualification, version.serving_status, version.deploy_failure_reason
    )
    # Distinct PROMOTION_DEPLOY_FAILED-class state — NOT a clean "promoted".
    assert state["state"] == "promotion_deploy_failed"
    assert state["serving_status"] == "deploy_failed"

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
    assert deploy_alerts[0].severity == AlertSeverity.ERROR

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
    assert verdict.post_promotion_outcome == "deploy_failed"


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
    """A deploy_failed resolution wakes the strategist: it arms exactly one
    coalescing strategic-refresh marker (qualification origin, deploy-failed
    reason) so recovery is not gated behind the strategic-spawn cooldown."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # already elapsed
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha="c" * 40,  # stale revision, never advanced
        deployed_at=promoted_at - timedelta(minutes=30),
    )

    resolved = await _confirm_serving_or_fail(
        version.id, session_factory=_shared_session_factory(db_session)
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
