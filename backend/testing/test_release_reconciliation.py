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
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app import release_version as rv
from app.models import Project
from app.release_promotion_pending import (
    _LEGACY_NATIVE_PROMOTION_PENDING_KEYS,
    _normalize_legacy_promotion_pending,
)
from app.release_reconciliation import (
    _PROMOTION_CLAIM_TIMEOUT_SECONDS,
    _promotion_pending_timeout_seconds,
    _reconcile_if_managed_check_wait,
    _reconcile_if_not_promoted_but_pr_merged,
    _reconcile_if_pending_qualification_superseded,
    _reconcile_if_promotion_failed_landed,
    _reconcile_if_promotion_pending_timed_out,
)
from app.time_utils import _utcnow


def _patch_timeout_closed_pr(mocker, *, pr_number: int = 99):
    """Make timeout cleanup observe an already-closed, unmerged PR."""

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": pr_number,
                "state": "closed",
                "merged": False,
                "head": {"ref": "release-v1", "sha": "abcd" * 10},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))


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
async def test_reconcile_promotion_failed_records_promotion_custom_branches(
    db_session, mocker
):
    """A failed record reconciles only from its exact merged release-PR receipt."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    candidate_sha = "abcd" * 10
    merge_sha = "merge001" * 5
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 42,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": merge_sha,
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
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
    # Receipt reachability targeted the configured promotion target, not "main".
    assert is_ancestor.await_args.args[4] == "release"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    # The qualifier's composite confidence must survive into the reconcile-
    # recorded promotion so the announcement / verdict report the true score.
    assert version.last_qualification["confidence"] == 0.95


@pytest.mark.asyncio
async def test_reconcile_promotion_pending_timeout_never_touches_main(
    db_session, mocker
):
    """A timeout clears only after its exact PR is confirmed terminal."""
    _patch_timeout_closed_pr(mocker)
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
            "managed_check_wait": True,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_enabled_at", [None, "not-a-timestamp"])
async def test_timeout_repairs_missing_or_invalid_pr_clock_and_persists_cleanup_debt(
    db_session, mocker, raw_enabled_at
):
    """Bad legacy clocks cannot disable timeout even when credentials are absent."""

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value=None),
    )
    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    started_at = (
        _utcnow() - timedelta(seconds=_promotion_pending_timeout_seconds() + 60)
    ).isoformat()
    pending = {
        "pr_number": 99,
        "pr_url": "https://github.com/acme/widget/pull/99",
    }
    if raw_enabled_at is not None:
        pending["enabled_at"] = raw_enabled_at
    version.last_qualification = {
        "status": "pending",
        "started_at": started_at,
        "candidate_sha": "abcd" * 10,
        "promotion_pending": pending,
    }
    db_session.add(version)
    await db_session.commit()

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert timed_out is False
    await db_session.refresh(version)
    canonical = version.last_qualification["promotion_pending"]
    assert canonical["enabled_at"] == started_at
    assert canonical["managed_check_wait"] is True
    assert canonical["cleanup_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [{}, "truthy-non-dict"])
async def test_timeout_clears_every_malformed_promotion_pending_shape(
    db_session, malformed
):
    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": malformed,
    }
    db_session.add(version)
    await db_session.commit()

    reconciled = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_pending_malformed"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_timeout_cleanup_debt_survives_failure_and_fences_redrive(
    db_session, mocker
):
    candidate_sha = "abcd" * 10
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 99,
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    close = mocker.patch(
        "app.github.close_pull_request",
        new=AsyncMock(side_effect=[False, True]),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/acme/widget/pull/99",
            "managed_check_wait": True,
            "enabled_at": (
                _utcnow()
                - timedelta(seconds=_promotion_pending_timeout_seconds() + 60)
            ).isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()

    first = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert first is False
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["promotion_pending"]["cleanup_required"] is True
    from app.release_qualifier import _promotion_pending_result

    assert _promotion_pending_result(version) is not None

    second = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert second is True
    assert close.await_count == 2
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "promotion_failed"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_timeout_racing_exact_merge_records_success_not_failure(db_session, mocker):
    candidate_sha = "abcd" * 10
    merge_sha = "d" * 40
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 99,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": merge_sha,
                "html_url": "https://github.com/acme/widget/pull/99",
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )

    async def reachable(_token, _owner, _repo, ancestor, _target, **_kwargs):
        return ancestor == merge_sha

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=reachable))
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    close = mocker.patch("app.github.close_pull_request", new=AsyncMock())
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/acme/widget/pull/99",
            "managed_check_wait": True,
            "enabled_at": (
                _utcnow()
                - timedelta(seconds=_promotion_pending_timeout_seconds() + 60)
            ).isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()

    reconciled = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    close.assert_not_awaited()
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_reconcile_promotion_claim_abandoned_short_timeout(db_session):
    """A bare promotion CLAIM orphaned by a promote() crash is GC'd on the SHORT
    claim bound, not the 24h open-PR cap.

    promote() stamps ``promotion_pending = {claiming: True, ...}`` under the row
    lock before its GitHub I/O so a concurrent re-qualify can't clobber the
    in-flight promotion. If promote() then crashes before opening the PR, the
    claim (which has NO pr_number) would otherwise inherit the 24h pending cap
    and wedge the version out of re-qualification for a day. The reaper must age
    a bare claim against ``_PROMOTION_CLAIM_TIMEOUT_SECONDS`` instead — clearing
    it back to stabilize so the version re-qualifies promptly. main is untouched.
    """
    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    # Older than the SHORT claim bound, but FAR younger than the 24h pending cap,
    # so only the claim-specific short bound can be what clears it.
    stranded_enabled = (
        _utcnow() - timedelta(seconds=_PROMOTION_CLAIM_TIMEOUT_SECONDS + 60)
    ).isoformat()
    assert _PROMOTION_CLAIM_TIMEOUT_SECONDS + 60 < _promotion_pending_timeout_seconds()
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "claiming": True,
            "candidate_sha": "abcd" * 10,
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
    assert version.last_qualification["error"] == "promotion_claim_abandoned"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_reconcile_fresh_promotion_claim_is_left_alone(db_session):
    """A FRESH claim (younger than the short bound) must NOT be cleared — the
    reaper cannot race a promote() whose GitHub I/O is legitimately still in
    flight, or it would tear down a live promotion mid-flight."""
    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    fresh_enabled = (_utcnow() - timedelta(seconds=5)).isoformat()
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "claiming": True,
            "candidate_sha": "abcd" * 10,
            "enabled_at": fresh_enabled,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert timed_out is False
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["promotion_pending"]["claiming"] is True


@pytest.mark.asyncio
async def test_reconcile_promotion_pending_spared_while_pipeline_paused(db_session):
    """PAUSE-AWARE 24h promotion_pending backstop: an open-PR promotion whose raw
    age exceeds the cap is NOT force-failed when the excess is pipeline-pause
    time. While PAUSED the promotion PR's CI/merge cannot progress, so failing it
    back to stabilize — which re-fires the whole qualify→promote triple, timing
    out again against the newest candidate for the pause's duration — is a false
    positive."""
    from app.models import DeploymentState, DeploymentStatus

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    # Armed 25h ago (1h past the 24h cap), but the pipeline has been PAUSED for
    # 24h of that → ~1h of ACTIVE age, under the cap.
    stranded_enabled = (
        _utcnow() - timedelta(seconds=_promotion_pending_timeout_seconds() + 3600)
    ).isoformat()
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "pr_number": 99,
            "pr_url": "https://github.com/acme/widget/pull/99",
            "managed_check_wait": True,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    db_session.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.PAUSED,
            paused_at=_utcnow()
            - timedelta(seconds=_promotion_pending_timeout_seconds()),
            paused_kind="manual",
        )
    )
    await db_session.commit()
    await db_session.refresh(version)

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert timed_out is False
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["promotion_pending"]["pr_number"] == 99


@pytest.mark.asyncio
async def test_reconcile_promotion_bare_claim_gc_even_while_paused(db_session):
    """The bare-claim GC path is deliberately NOT pause-aware: a claim orphaned by
    a crashed promote() will not self-heal on resume, so it is cleared on RAW age
    against the short claim bound even while the pipeline is PAUSED — otherwise a
    crashed promotion would wedge the version out of re-qualification for the
    entire pause. A long open pause here WOULD spare it if the bare-claim path
    were (wrongly) pause-aware; it must be GC'd regardless."""
    from app.models import DeploymentState, DeploymentStatus

    _project, version = await _make_project_and_version(
        db_session, integration_branch="trunk", promotion_target="release"
    )
    stranded_enabled = (
        _utcnow() - timedelta(seconds=_PROMOTION_CLAIM_TIMEOUT_SECONDS + 60)
    ).isoformat()
    assert _promotion_pending_timeout_seconds() > _PROMOTION_CLAIM_TIMEOUT_SECONDS + 60
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "claiming": True,
            "candidate_sha": "abcd" * 10,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    db_session.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.PAUSED,
            paused_at=_utcnow() - timedelta(seconds=_PROMOTION_CLAIM_TIMEOUT_SECONDS),
            paused_kind="manual",
        )
    )
    await db_session.commit()
    await db_session.refresh(version)

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert timed_out is True
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_claim_abandoned"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_reconcile_promotion_pending_times_out_despite_brief_pause(
    db_session, mocker
):
    """MUTATION-PROOF companion to the pause-spare test: the pause-aware promotion
    backstop must not become a blanket disable. An open-PR promotion 25h past
    arming with only a 30-min pause still has ~24.5h of ACTIVE age above the 24h
    cap and IS failed back to stabilize."""
    from app.models import DeploymentState, DeploymentStatus

    _patch_timeout_closed_pr(mocker)

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
            "managed_check_wait": True,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    db_session.add(
        DeploymentState(
            id=1,
            status=DeploymentStatus.PAUSED,
            paused_at=_utcnow() - timedelta(minutes=30),
            paused_kind="manual",
        )
    )
    await db_session.commit()
    await db_session.refresh(version)

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert timed_out is True
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_pending_timeout"


# ===========================================================================
# Incident 2026-06-09: not_promoted/ci_unresolved reconciliation net.
#
# A transient DB error can roll back the promotion_pending marker write after
# a release PR is created and the managed waiter marker is being persisted. The
# subsequent finalize
# sweep sees no promotion_pending, re-evaluates CI as ci_unresolved (transient,
# not a hard failure), and records not_promoted — even though the PR merged
# moments later.  _reconcile_if_not_promoted_but_pr_merged catches this.
# ===========================================================================


@pytest.mark.asyncio
async def test_reconcile_not_promoted_ci_unresolved_candidate_on_main_reconciles(
    db_session, mocker
):
    """A transient ci_unresolved miss recovers from its exact merged PR receipt."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    candidate_sha = "abcd" * 10
    merge_sha = "merge007" * 5
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 7,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": merge_sha,
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
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
async def test_reconcile_not_promoted_ci_unresolved_armed_merge_grade_propagates(
    db_session, mocker
):
    """_reconcile_if_not_promoted_but_pr_merged: a not_promoted/ci_unresolved
    record carrying merge_grade_misses=True reconciles to promoted AND the
    armed merge-grade signal propagates through _recover_confidence_from_ci_unresolved
    into the final promotion record.

    Regression guard: _recover_confidence_from_ci_unresolved calls
    _merge_grade_misses_armed which checks base_record["merge_grade_misses"]
    first (before the policy threshold).  A stored True must survive the
    recovery path so the promoted record reflects the actual merge-grade
    verdict, not a silently-laundered clean bill of health.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    candidate_sha = "abcd" * 10
    merge_sha = "merge009" * 5
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 9,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": merge_sha,
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
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
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_unresolved",
        "candidate_sha": candidate_sha,
        "confidence": 0.0,
        "merge_grade_misses": True,
        "pr_url": "https://github.com/acme/widget/pull/9",
        "sub_scores": {"audit": 0.91, "test_erosion": 0.85, "smoketest": 0.0},
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
    qual = version.last_qualification
    assert qual["status"] == "promoted"
    # _recover_confidence_from_ci_unresolved re-runs compute_composite with
    # merge_grade_misses=True (because base_record["merge_grade_misses"] is
    # True, _merge_grade_misses_armed short-circuits to True).  compute_composite
    # with merge_grade_misses=True returns blocker="merge_grade_misses" —
    # the signal is NOT laundered away during reconciliation.
    assert qual.get("blocker") == "merge_grade_misses"
    assert qual.get("confidence") == pytest.approx(0.0)
    assert qual.get("confidence_recovered_from_ci_unresolved") is True


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
    PoolVm,
    PoolVmState,
    ProjectMessage,
    QualificationVerdict,
    ReleaseDeployArtifact,
    ReleaseVersion,
)
from app import release_artifacts as artifact_registry  # noqa: E402
from app.release_artifacts import (  # noqa: E402
    AGENT_IMAGE_ARTIFACT_KIND,
    BACKEND_REVISION_ARTIFACT_KIND,
    PROMOTION_MERGE_ARTIFACT_KIND,
    ReleaseArtifactEvidence,
    ReleaseArtifactProvider,
    SUPERSEDED_BY_DESCENDANT_REASON,
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
_SHA_C = "c" * 40


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
    """A STABLE revision whose serving SHA is UNOBSERVABLE and predates the
    promotion never yields a decision; past the deadline the classifier lands
    confirmation_timed_out/no_new_revision (no active failure observed).  A
    STABLE revision serving an OBSERVABLE mismatched SHA now emits sha_mismatch
    (delegated to the consumer-layer descendant check), so this no-new-revision
    timeout path is exercised through the unobservable pre-promotion case."""
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deadline = datetime(2026, 6, 5, 13, 0, 0)
    # Pre-promotion STABLE revision whose serving SHA could not be read.
    ds = _ds(
        DeploymentStatus.STABLE, datetime(2026, 6, 5, 11, 50, 0), current_sha=""
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
    # Pre-promotion STABLE revision whose serving SHA is unobservable and which
    # never advances; deadline=None.  (An observable mismatched SHA now routes
    # to sha_mismatch, so the deadline backstop is exercised via this path.)
    ds = _ds(
        DeploymentStatus.STABLE, datetime(2026, 6, 5, 11, 50, 0), current_sha=""
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


def test_classify_stable_pre_promotion_sha_mismatch_routes_to_descendant_check():
    """A pre-promotion revision (deployed_at < promoted_at) serving a different
    SHA is AMBIGUOUS: it may be the OLD revision still serving (deploy in
    flight) OR the deploy landing as the promotion MERGE COMMIT — a descendant
    of promoted_sha that was observed booting before the orchestrator stamped
    promoted_at (the v25/v27 merge-commit promotion pattern).  The classifier
    therefore emits sha_mismatch REGARDLESS of ordering so the consumer-layer
    descendant check (_reclassify_sha_mismatch_if_superseded) can resolve the
    ambiguity: it confirms a descendant and keeps a diverged/behind/unrelated
    pre-promotion revision waiting (then timing out honestly)."""
    promoted_at = datetime(2026, 6, 5, 12, 0, 0)
    deployed_at = datetime(2026, 6, 5, 11, 50, 0)  # predates promotion
    deadline = promoted_at + timedelta(hours=1)
    now = promoted_at + timedelta(minutes=15)
    ds = _ds(DeploymentStatus.STABLE, deployed_at, current_sha=_SHA_B)
    out = _classify_serving_outcome(ds, promoted_at, _SHA_A, deadline, now)
    assert out == ("deploy_failed", "sha_mismatch", None)


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
    receipt = (
        await db_session.execute(
            _select(ProjectMessage).where(
                ProjectMessage.project_id == version.project_id,
                ProjectMessage.idempotency_key
                == f"release:{version.id}:serving:confirmed",
            )
        )
    ).scalar_one()
    assert receipt.context["event_type"] == "release.serving_confirmed"
    assert receipt.context["served_sha"] == _SHA_A


@pytest.mark.asyncio
async def test_confirm_backend_revision_descendant_serving_sha_records_observed_sha(
    db_session, mocker
):
    """A STABLE post-promotion revision serving a descendant of promoted_sha is
    healthy, and the provider records the observed serving SHA rather than
    substituting the promoted SHA."""
    ancestry = mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )

    assert resolved is True
    ancestry.assert_awaited()
    assert ancestry.await_args.args[3:5] == (_SHA_A, _SHA_B)
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_B
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
    assert artifact.serving_status == "serving_confirmed"
    assert artifact.served_identifier == _SHA_B


@pytest.mark.asyncio
async def test_confirm_backend_revision_non_ancestor_sha_mismatch_stays_failed(
    db_session, mocker
):
    ancestry = mocker.patch(
        "app.github.is_ancestor_of",
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
        current_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )

    assert resolved is True
    ancestry.assert_awaited()
    await db_session.refresh(version)
    assert version.serving_status == "deploy_failed"
    assert version.deploy_failure_reason == "sha_mismatch"
    assert version.served_sha is None


@pytest.mark.asyncio
async def test_confirm_backend_revision_indeterminate_ancestry_defers_within_window(
    db_session, mocker
):
    mocker.patch(
        "app.github.is_ancestor_of",
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
        current_sha=_SHA_B,
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
    assert version.deploy_failure_reason is None


@pytest.mark.asyncio
async def test_confirm_backend_revision_indeterminate_ancestry_times_out_after_deadline(
    db_session, mocker
):
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=None),
    )
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(hours=2),
    )

    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "confirmation_timed_out"
    assert version.deploy_failure_reason == "backend_revision_ancestry_indeterminate"
    assert version.served_sha is None


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
    assert any("synthetic_required" in (alert.message or "") for alert in alerts), (
        "deploy-failed alert should describe the failed required artifact"
    )


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
async def test_confirm_no_new_revision_times_out_scenario(db_session, mocker):
    """(b) The live revision never advances within the window → CONFIRMATION
    TIMED OUT, distinct from deploy_failed: nothing was observed going bad, so NO
    loud ERROR alert fires and the verdict outcome is confirmation_timed_out. The
    never-confirmed promotion is reported as a distinct state, not cleanly
    shipped and not a crisis.

    The stale pre-promotion revision now emits sha_mismatch (the descendant
    check runs for every STABLE mismatch); because it PREDATES the promotion and
    is determinately NOT a descendant, the supersession check keeps the
    conservative wait → honest-timeout path instead of hard-failing."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # already elapsed
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # Stale revision predating the promotion; STABLE but never advanced, and
    # determinately NOT a descendant of the promoted commit.  No deploy.yml run
    # exists for the promoted SHA, so the timeout is substantiated as
    # never-dispatched rather than the bare no_new_revision backstop.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_workflow_run_for_sha", new=AsyncMock(return_value=None)
    )
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
    assert version.deploy_failure_reason == "deploy_workflow_never_dispatched"
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
    timeout_receipt = (
        await db_session.execute(
            _select(ProjectMessage).where(
                ProjectMessage.project_id == version.project_id,
                ProjectMessage.idempotency_key
                == f"release:{version.id}:serving:timed_out",
            )
        )
    ).scalar_one()
    assert (
        timeout_receipt.context["event_type"]
        == "release.serving_confirmation_timed_out"
    )


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
    failure_receipt = (
        await db_session.execute(
            _select(ProjectMessage).where(
                ProjectMessage.project_id == version.project_id,
                ProjectMessage.idempotency_key
                == f"release:{version.id}:serving:failed",
            )
        )
    ).scalar_one()
    assert failure_receipt.context["event_type"] == "release.serving_failed"
    assert failure_receipt.context["reason"] == "rolled_back"


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
async def test_confirm_timed_out_arms_strategic_refresh_distinctly(db_session, mocker):
    """A confirmation TIMEOUT also wakes the strategist, but with a DISTINCT
    reason (promotion_confirmation_timed_out:...) — never the deploy_failed crisis
    reason — so the handoff reads as 'verify serving', not 'recover a failed
    deploy'."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # already elapsed
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # Stale revision that never advanced → no active failure → timeout.  The
    # pre-promotion divergent ancestry keeps the wait → honest-timeout path.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
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
async def test_recover_deploy_failed_when_descendant_sha_serves_within_window(
    db_session, mocker
):
    """A sha_mismatch deploy_failed latch recovers when the observed serving SHA
    is deterministically descended from the promoted SHA."""
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    now = _utcnow()
    promoted_at = now - timedelta(hours=5)
    deadline = promoted_at + timedelta(hours=1)
    alerted_at = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_deploy_failed_version(
        db_session,
        promoted_at=promoted_at,
        deadline=deadline,
        alerted_at=alerted_at,
        deploy_failure_reason="sha_mismatch",
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
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
    assert version.served_sha == _SHA_B
    assert version.deploy_failure_reason is None


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
async def test_recover_skips_when_sha_never_serves(db_session, mocker):
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
    # Stale revision predating the promotion; STABLE but never advanced, and
    # determinately NOT a descendant → the re-check keeps it non-recovered.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
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
async def test_recover_exact_sha_after_recovery_window(db_session):
    """A deploy whose promoted SHA is now DETERMINISTICALLY observed serving
    recovers even AFTER the old recovery window has elapsed: the recovery gate is
    the truth recheck (a serving_confirmed aggregate), not a timer.  Recording the
    failure far in the past no longer condemns a deploy that is genuinely live."""
    now = _utcnow()
    # Failure was recorded well beyond the old recovery window.
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
    # The promoted SHA is NOW genuinely serving (exact match); the elapsed window
    # must no longer suppress the recheck.
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


@pytest.mark.asyncio
async def test_recover_deploy_failed_descendant_after_recovery_window(
    db_session, mocker
):
    """A sha_mismatch deploy_failed latch recovers when the observed serving SHA
    is an ancestry-verified DESCENDANT of the promoted SHA — even though the old
    24h recovery window has long since elapsed.

    This is the v9 case: the promoted merge SHA served, then a newer main deploy
    containing a descendant superseded it; the latch must clear once the
    descendant-aware provider confirms serving, regardless of elapsed time."""
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    now = _utcnow()
    # Failure recorded well beyond the old recovery window.
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
        deploy_failure_reason="sha_mismatch",
    )
    # A descendant of the promoted SHA is now serving.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
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
    assert version.served_sha == _SHA_B
    assert version.deploy_failure_reason is None

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

    # Strategic refresh armed so the crisis directive recomputes off the truth.
    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is True


@pytest.mark.asyncio
async def test_recover_skips_beyond_window_determinate_non_ancestry(db_session, mocker):
    """A sha_mismatch latch whose observed serving SHA is DETERMINISTICALLY not
    descended from the promoted SHA stays deploy_failed beyond the recovery
    window: an elapsed window must never force-clear a determinate negative."""
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )
    now = _utcnow()
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
        deploy_failure_reason="sha_mismatch",
    )
    # An unrelated SHA is serving and ancestry is determinately false.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
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

    # The open deploy-failed alert is untouched and no refresh was armed.
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


# ===========================================================================
# Supersession by a descendant — sha_mismatch vs a legitimately advanced branch.
#
# The classifier's exact current_sha == promoted_sha match falsely latches
# deploy_failed/sha_mismatch when main legitimately advances PAST the promoted
# commit before confirmation resolves (the real v9 incident: the promoted merge
# SHA verifiably served, then a DESCENDANT commit containing it deployed before
# the sweep first checked).  On the sha_mismatch path only, the backend
# provider asks GitHub (compare API: base = promoted SHA, head = observed live
# SHA) whether the promoted commit is an ancestor of what is serving:
# descendant → serving_confirmed with the OBSERVED SHA + the distinct
# superseded_by_descendant reason; diverged/unrelated or ANY GitHub failure →
# today's deploy_failed/sha_mismatch exactly.
# ===========================================================================


async def _backend_artifact_for(db_session, version):
    return (
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


@pytest.mark.asyncio
async def test_confirm_supersession_by_descendant_resolves_serving_confirmed(
    db_session, mocker
):
    """A STABLE revision serving a DESCENDANT of the promoted SHA is a succeeded,
    superseded deploy — serving_confirmed with the OBSERVED live SHA (never the
    promoted SHA echoed) and the distinct superseded_by_descendant reason; no
    deploy-failed ERROR alert."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # The live revision serves a different SHA that CONTAINS the promoted commit.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=40),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"
    # The served SHA is the OBSERVED live SHA — never the promoted SHA.
    assert version.served_sha == _SHA_B
    assert version.served_at is not None
    assert version.deploy_failure_reason is None

    # Ancestry was asked exactly once: base = promoted SHA, head = observed SHA.
    is_ancestor.assert_awaited_once_with(
        "tok",
        "acme",
        "widget",
        _SHA_A,
        _SHA_B,
        project_id=str(version.project_id),
    )

    artifact = await _backend_artifact_for(db_session, version)
    assert artifact.serving_status == "serving_confirmed"
    assert artifact.served_identifier == _SHA_B
    assert artifact.reason_code == SUPERSEDED_BY_DESCENDANT_REASON

    # No loud deploy-failed ERROR alert for a deploy that succeeded.
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


@pytest.mark.asyncio
async def test_confirm_diverged_sha_stays_deploy_failed_sha_mismatch(
    db_session, mocker
):
    """A live SHA that does NOT contain the promoted commit (diverged / behind /
    unrelated) keeps today's deploy_failed/sha_mismatch exactly, alert included."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=40),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "deploy_failed"
    assert version.deploy_failure_reason == "sha_mismatch"
    assert version.served_sha is None

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
@pytest.mark.parametrize(
    "ancestry_failure", ["raises", "indeterminate", "no_credentials"]
)
async def test_confirm_github_failure_falls_back_to_sha_mismatch(
    db_session, mocker, ancestry_failure
):
    """Any GitHub/compare failure on the supersession check never silently confirms.

    ``raises`` and ``no_credentials`` still land deploy_failed/sha_mismatch (they
    bypass the indeterminate defer path via the exception handler and the
    no-credentials early return respectively).  An ``indeterminate`` response
    (GitHub returned a valid but inconclusive answer) defers within the window and
    times out to confirmation_timed_out past the deadline — a soft non-confirmation,
    not deploy_failed, but still not a silent serving_confirmed."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # deadline is 1 h ago
    if ancestry_failure == "raises":
        mocker.patch(
            "app.github.is_ancestor_of",
            new=AsyncMock(side_effect=RuntimeError("github exploded")),
        )
        # Exception path falls back immediately to deploy_failed/sha_mismatch.
        expected_status = "deploy_failed"
        expected_reason = "sha_mismatch"
    elif ancestry_failure == "indeterminate":
        mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=None))
        # Past deadline: indeterminate becomes confirmation_timed_out (soft non-confirm).
        expected_status = "confirmation_timed_out"
        expected_reason = "backend_revision_ancestry_indeterminate"
    else:  # no_credentials — the compare API must never even be attempted.
        mocker.patch(
            "app.release_promotion._resolve_github_with_session",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.github.is_ancestor_of",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        )
        # No-credentials early return falls back to deploy_failed/sha_mismatch.
        expected_status = "deploy_failed"
        expected_reason = "sha_mismatch"

    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(hours=2),  # well past deadline
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == expected_status
    assert version.deploy_failure_reason == expected_reason
    assert version.served_sha is None


@pytest.mark.asyncio
async def test_confirm_merge_commit_descendant_deployed_before_promoted_at_confirms(
    db_session, mocker
):
    """The v25/v27 merge-commit promotion: deploy.yml deploys github.sha (the
    MERGE COMMIT) while version.promoted_sha records the automain tip, so the
    exact-match path can never fire.  When the observed STABLE revision is a
    DESCENDANT of promoted_sha and the deploy was observed BEFORE the
    orchestrator stamped promoted_at (deployed_at <= promoted_at), the version
    must CONFIRM serving with the OBSERVED SHA — this is the case the ordering
    gate used to skip straight into confirmation_timed_out."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    # The merge commit (a descendant of promoted_sha) booted BEFORE the
    # orchestrator recorded promoted_at — the v27 structural pattern.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=promoted_at - timedelta(hours=2),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=40),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"
    # The served SHA is the OBSERVED live SHA (the merge commit), never the
    # promoted automain-tip SHA echoed as a stand-in.
    assert version.served_sha == _SHA_B
    assert version.deploy_failure_reason is None

    # Ancestry was asked exactly once with base = promoted SHA, head = observed.
    is_ancestor.assert_awaited_once_with(
        "tok",
        "acme",
        "widget",
        _SHA_A,
        _SHA_B,
        project_id=str(version.project_id),
    )

    artifact = await _backend_artifact_for(db_session, version)
    assert artifact.serving_status == "serving_confirmed"
    assert artifact.served_identifier == _SHA_B
    assert artifact.reason_code == SUPERSEDED_BY_DESCENDANT_REASON


@pytest.mark.asyncio
async def test_confirm_pre_promotion_diverged_sha_waits_then_times_out(
    db_session, mocker
):
    """A pre-promotion revision that is determinately NOT a descendant
    (diverged / behind / unrelated) must NEVER silently confirm.  Within the
    window the version stays unconfirmed (defer — the deploy may still land);
    past the deadline it times out honestly as confirmation_timed_out — the
    "genuinely absent revision still times out honestly" guarantee now that the
    classifier routes every STABLE mismatch through the descendant check."""
    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_C,
        deployed_at=promoted_at - timedelta(hours=2),  # predates promotion
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    # No deploy.yml run for the promoted SHA — the past-deadline timeout is
    # substantiated as never-dispatched, never deploy_failed / never confirmed.
    mocker.patch(
        "app.github.get_workflow_run_for_sha", new=AsyncMock(return_value=None)
    )

    # Within the confirmation window: no decision, version stays unconfirmed.
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=30),
    )
    assert resolved is False
    await db_session.refresh(version)
    assert version.serving_status == "unconfirmed"
    assert version.served_sha is None

    # Past the deadline: honest confirmation_timed_out, never deploy_failed and
    # never serving_confirmed.
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(hours=2),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "confirmation_timed_out"
    assert version.deploy_failure_reason == "deploy_workflow_never_dispatched"
    assert version.served_sha is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raises", "no_credentials", "indeterminate"])
async def test_confirm_pre_promotion_github_failure_never_confirms(
    db_session, mocker, failure
):
    """A GitHub API / credential failure on the descendant check for a
    PRE-promotion STABLE mismatch must never silently confirm.  ``raises`` and
    ``no_credentials`` fall back to deploy_failed/sha_mismatch; an
    ``indeterminate`` answer defers within the window and times out past the
    deadline — all non-confirmations."""
    promoted_at = _utcnow() - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)  # already elapsed
    if failure == "raises":
        mocker.patch(
            "app.github.is_ancestor_of",
            new=AsyncMock(side_effect=RuntimeError("github exploded")),
        )
        expected_status = "deploy_failed"
        expected_reason = "sha_mismatch"
    elif failure == "indeterminate":
        mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=None))
        expected_status = "confirmation_timed_out"
        expected_reason = "backend_revision_ancestry_indeterminate"
    else:  # no_credentials — the compare API must never even be attempted.
        mocker.patch(
            "app.release_promotion._resolve_github_with_session",
            new=AsyncMock(return_value=None),
        )
        mocker.patch(
            "app.github.is_ancestor_of",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        )
        expected_status = "deploy_failed"
        expected_reason = "sha_mismatch"

    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=promoted_at - timedelta(minutes=30),  # predates promotion
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(hours=2),  # well past deadline
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == expected_status
    assert version.deploy_failure_reason == expected_reason
    assert version.served_sha is None


@pytest.mark.asyncio
async def test_recover_confirmation_timed_out_when_descendant_deployed_before_promoted_at(
    db_session, mocker
):
    """A version LATCHED confirmation_timed_out becomes re-checkable and
    recoverable: once the live revision is observed to be a DESCENDANT of
    promoted_sha — even though it booted before promoted_at was stamped (the
    v27 merge-commit ordering that the old gate sent to an unrecoverable
    timeout) — the recovery sweep re-confirms it serving_confirmed with the
    observed SHA, clearing the timeout latch and the verdict outcome."""
    now = _utcnow()
    promoted_at = now - timedelta(hours=5)
    project = Project(
        name="Recover-Timeout-Desc",
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
    version.deploy_failure_reason = "no_new_revision"
    version.deploy_confirm_deadline = promoted_at + timedelta(hours=1)
    version.last_qualification = {"status": "promoted", "promoted_sha": _SHA_A}
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    db_session.add(
        QualificationVerdict(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=_SHA_A,
            terminal_status="promoted",
            would_promote=True,
            promotion_enabled=True,
            post_promotion_outcome="confirmation_timed_out",
            post_promotion_outcome_at=promoted_at + timedelta(hours=1),
        )
    )
    db_session.add(
        ReleaseDeployArtifact(
            version_id=version.id,
            artifact_kind=BACKEND_REVISION_ARTIFACT_KIND,
            serving_status="confirmation_timed_out",
            reason_code="no_new_revision",
            confirmation_deadline=promoted_at + timedelta(hours=1),
        )
    )
    await db_session.commit()

    # The merge commit (a descendant of promoted_sha) is serving, booted BEFORE
    # the orchestrator stamped promoted_at.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=promoted_at - timedelta(hours=2),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))

    recovered = await _reconcile_if_deploy_failed_recovered(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=now,
    )
    assert recovered is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_B
    assert version.deploy_failure_reason is None

    artifact = await _backend_artifact_for(db_session, version)
    assert artifact.serving_status == "serving_confirmed"
    assert artifact.served_identifier == _SHA_B
    assert artifact.reason_code == SUPERSEDED_BY_DESCENDANT_REASON

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


@pytest.mark.asyncio
async def test_recover_latched_sha_mismatch_when_live_sha_is_descendant(
    db_session, mocker
):
    """The v9 self-heal: a version already LATCHED deploy_failed/sha_mismatch
    recovers to serving_confirmed within the recovery window once the live SHA is
    recognized as a descendant — served SHA = observed SHA, the distinct
    superseded_by_descendant reason survives recovery, the open ERROR alert is
    dismissed, and the verdict outcome flips."""
    now = _utcnow()
    promoted_at = now - timedelta(hours=5)
    deadline = promoted_at + timedelta(hours=1)
    alerted_at = promoted_at + timedelta(hours=1)  # within the 24h recovery window
    project, version = await _make_promoted_deploy_failed_version(
        db_session,
        promoted_at=promoted_at,
        deadline=deadline,
        alerted_at=alerted_at,
        deploy_failure_reason="sha_mismatch",
    )
    # The live revision serves a DESCENDANT of the promoted SHA.
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=now - timedelta(minutes=10),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))

    recovered = await _reconcile_if_deploy_failed_recovered(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=now,
    )
    assert recovered is True
    await db_session.refresh(version)
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_B
    assert version.served_at is not None
    assert version.deploy_failure_reason is None

    artifact = await _backend_artifact_for(db_session, version)
    assert artifact.serving_status == "serving_confirmed"
    assert artifact.served_identifier == _SHA_B
    assert artifact.reason_code == SUPERSEDED_BY_DESCENDANT_REASON

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

    # The strategist is woken so the crisis directive recomputes.
    await db_session.refresh(project)
    assert strategic_refresh_pending(project.vision) is True
    marker = project.vision["_strategic_refresh"]
    assert str(marker.get("reason", "")) == "promotion_deploy_recovered"


@pytest.mark.asyncio
async def test_recover_latched_sha_mismatch_stays_failed_when_diverged(
    db_session, mocker
):
    """A latched sha_mismatch whose live SHA does NOT contain the promoted commit
    stays deploy_failed through the recovery sweep: alert untouched, no refresh."""
    now = _utcnow()
    promoted_at = now - timedelta(hours=5)
    deadline = promoted_at + timedelta(hours=1)
    alerted_at = promoted_at + timedelta(hours=1)
    project, version = await _make_promoted_deploy_failed_version(
        db_session,
        promoted_at=promoted_at,
        deadline=deadline,
        alerted_at=alerted_at,
        deploy_failure_reason="sha_mismatch",
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        deployed_at=now - timedelta(minutes=10),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))

    recovered = await _reconcile_if_deploy_failed_recovered(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=now,
    )
    assert recovered is False
    await db_session.refresh(version)
    assert version.serving_status == "deploy_failed"
    assert version.deploy_failure_reason == "sha_mismatch"
    assert version.served_sha is None

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
    rows = [
        (providers[a.artifact_kind], a)
        for a in artifacts
        if a.artifact_kind in providers
    ]
    status, _reason = aggregate_required_artifact_status(rows)
    return status


def _timed_out_row(
    artifact_kind: str, reason_code: str | None
) -> tuple[ReleaseArtifactProvider, ReleaseArtifactEvidence]:
    """Build a (provider, evidence) pair for a confirmation_timed_out artifact."""
    provider = ReleaseArtifactProvider(
        artifact_kind=artifact_kind, check=_async_noop
    )
    evidence = ReleaseArtifactEvidence(
        artifact_kind=artifact_kind,
        required=True,
        serving_status="confirmation_timed_out",
        served_identifier=None,
        served_at=None,
        reason_code=reason_code,
    )
    return provider, evidence


async def _async_noop(*_args, **_kwargs):  # pragma: no cover - callable shape only
    return None


def test_aggregate_reason_reports_actual_blocking_gate():
    """When several required artifacts time out together, the aggregate reason
    must reflect the REAL unsatisfied gate, not registry-first ordering.  A
    backend ``no_new_revision`` (the registry-first artifact) is a
    low-information backstop that must not mask a concrete blocker like
    ``agent_image_fleet_not_yet_serving`` — otherwise a live-serving backend
    gets mislabeled ``no_new_revision`` and the operator cannot see the fleet
    gate that is actually blocking confirmation."""
    rows = [
        _timed_out_row(BACKEND_REVISION_ARTIFACT_KIND, "no_new_revision"),
        _timed_out_row(AGENT_IMAGE_ARTIFACT_KIND, "agent_image_fleet_not_yet_serving"),
        (
            ReleaseArtifactProvider(
                artifact_kind=PROMOTION_MERGE_ARTIFACT_KIND, check=_async_noop
            ),
            ReleaseArtifactEvidence(
                artifact_kind=PROMOTION_MERGE_ARTIFACT_KIND,
                required=True,
                serving_status="serving_confirmed",
                served_identifier=None,
                served_at=None,
                reason_code=None,
            ),
        ),
    ]
    status, reason = aggregate_required_artifact_status(rows)
    assert status == "confirmation_timed_out"
    # The concrete fleet gate surfaces over the registry-first no_new_revision
    # backstop — the actual blocker, not the first-timed-out artifact.
    assert reason == "agent_image_fleet_not_yet_serving"


def test_aggregate_reason_falls_back_to_backstop_when_all_no_new_revision():
    """When every timed-out artifact is the low-information backstop (no more
    specific signal exists), the aggregate falls back to the first reason
    rather than inventing one — single-gate and all-backstop cases keep their
    historical reason."""
    rows = [_timed_out_row(BACKEND_REVISION_ARTIFACT_KIND, "no_new_revision")]
    status, reason = aggregate_required_artifact_status(rows)
    assert status == "confirmation_timed_out"
    assert reason == "no_new_revision"


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


def test_previously_deployed_sha_uses_prior_when_current_contains_promoted():
    """When the observed backend SHA is a descendant of the promoted SHA, the
    bake diff still starts at the pre-promotion deploy SHA."""
    ds = DeploymentState(
        id=1, current_sha=_SHA_B, prior_deploy_sha=_SHA_C, deployed_at=_utcnow()
    )
    assert (
        _previously_deployed_sha(ds, _SHA_A, current_contains_promoted=True) == _SHA_C
    )


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
    assert (
        await _aggregate_serving_status(db_session, version.id) == "serving_confirmed"
    )


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_bake_path_confirms_with_descendant_fleet_sha(
    db_session, mocker
):
    """A bake-path promotion confirms when a live fleet VM reports a source SHA
    that deterministically descends from promoted_sha, ignoring other projects."""
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=True),
    )
    ancestry = mocker.patch(
        "app.github_refs.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    unrelated_project = Project(
        name="Other serving test",
        user_id="other-serving-user",
        github_owner="other-acme",
        github_repo="other-widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(unrelated_project)
    await db_session.commit()
    await db_session.refresh(unrelated_project)
    db_session.add(
        PoolVm(
            vm_instance_id="pool-descendant",
            project_id=project.id,
            state=PoolVmState.IDLE,
            agent_source_sha=_SHA_B,
        )
    )
    # A matching SHA from another project must not confirm this project's
    # agent-image artifact. Before project scoping, it could short-circuit the
    # descendant proof below depending on query iteration order.
    db_session.add(
        PoolVm(
            vm_instance_id="pool-unrelated-promoted",
            project_id=unrelated_project.id,
            state=PoolVmState.IDLE,
            agent_source_sha=_SHA_A,
        )
    )
    # The short-lived heartbeat cache has no project identity of its own. An
    # unowned cached SHA must not be allowed to confirm this project's artifact.
    from app import compute

    mocker.patch.dict(compute._vm_source_shas, {"foreign-cache": _SHA_A})
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        prior_deploy_sha=_SHA_C,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )

    assert resolved is True
    ancestry.assert_awaited()
    assert ancestry.await_args.args[3:5] == (_SHA_A, _SHA_B)
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


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_descendant_fleet_indeterminate_stays_unconfirmed(
    db_session, mocker
):
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new=AsyncMock(return_value=None),
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    db_session.add(
        PoolVm(
            vm_instance_id="pool-indeterminate",
            project_id=project.id,
            state=PoolVmState.IDLE,
            agent_source_sha=_SHA_B,
        )
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        prior_deploy_sha=_SHA_C,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )

    assert resolved is False
    assert await _aggregate_serving_status(db_session, version.id) == "unconfirmed"


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_bake_diff_uses_prior_when_backend_sha_is_ahead(
    db_session, mocker
):
    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    rebake = mocker.patch(
        "app.github_refs.agent_image_rebake_expected",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.github_refs.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )
    # Backend-revision sha_mismatch path calls app.github.is_ancestor_of (distinct
    # from app.github_refs) to check descendant supersession.  Mock it here so the
    # backend_revision provider produces a definitive result without hitting the
    # real GitHub API.
    mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=True),
    )

    promoted_at = _utcnow()
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_B,
        prior_deploy_sha=_SHA_C,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )

    assert resolved is True
    rebake.assert_awaited_once()
    assert rebake.await_args.args[3:5] == (_SHA_C, _SHA_A)


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
    NOT-YET-CONFIRMED, never deploy_failed — within the window the aggregate
    waits, and past the deadline the artifact times out to
    confirmation_timed_out (a soft "verify serving", NOT deploy_failed)."""
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

    # Past the deadline the agent_image gate times out (confirmation_timed_out,
    # NOT deploy_failed) so the version resolves instead of wedging unconfirmed.
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(hours=2),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "confirmation_timed_out"
    assert version.deploy_failure_reason == "agent_image_rebake_lookup_indeterminate"
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
    assert artifact.serving_status == "confirmation_timed_out"
    assert artifact.reason_code == "agent_image_rebake_lookup_indeterminate"


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_indeterminate_no_fleet_reported_stays_unconfirmed(
    db_session, mocker
):
    """(c) A bake IS expected but no fleet VM has reported a source SHA yet —
    the fleet observation is indeterminate → not-yet-confirmed, never
    deploy_failed.  Past the confirmation deadline the artifact times out to
    confirmation_timed_out so the version resolves instead of wedging."""
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
    assert resolved is True
    await db_session.refresh(version)
    # Aggregate resolves to confirmation_timed_out (never deploy_failed) — an
    # indeterminate fleet observation ≠ failed deploy, but it must not wedge the
    # version unconfirmed forever.
    assert version.serving_status == "confirmation_timed_out"
    assert version.deploy_failure_reason == "agent_image_fleet_not_yet_serving"
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
    assert artifact.serving_status == "confirmation_timed_out"
    assert artifact.reason_code == "agent_image_fleet_not_yet_serving"


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_indeterminate_past_deadline_times_out(
    db_session, mocker
):
    """A bake-path agent_image whose fleet never reports serving the promoted
    SHA stays unconfirmed WITHIN the confirmation window, then TIMES OUT past
    the confirmation_deadline (confirmation_timed_out, never deploy_failed) so
    the version resolves and the recovery sweep can later re-check it.  This is
    the deadline branch the provider was missing: before it, the aggregate
    stayed unconfirmed forever and no recovery ever engaged."""
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
    await _set_deploy_state(
        db_session,
        status=DeploymentStatus.STABLE,
        current_sha=_SHA_A,
        prior_deploy_sha=_SHA_B,
        deployed_at=promoted_at + timedelta(minutes=5),
    )

    # Within the window: the indeterminate fleet signal defers — the aggregate
    # stays unconfirmed and the version does not yet resolve.  (The legacy
    # version.serving_status scalar mirrors the backend artifact and reads
    # serving_confirmed here; the OPERATOR-FACING truth is the aggregate.)
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(minutes=20),
    )
    assert resolved is False
    await db_session.refresh(version)
    assert await _aggregate_serving_status(db_session, version.id) == "unconfirmed"
    assert version.deploy_failure_reason is None

    # Past the deadline: the fleet STILL has not served the promoted SHA, so the
    # agent_image artifact times out.  The backend revision already confirmed
    # (current_sha == promoted_sha), so the aggregate resolves to
    # confirmation_timed_out and the version leaves unconfirmed — no longer
    # wedged forever.
    resolved = await _confirm_serving_or_fail(
        version.id,
        session_factory=_shared_session_factory(db_session),
        now=promoted_at + timedelta(hours=2),
    )
    assert resolved is True
    await db_session.refresh(version)
    assert version.serving_status == "confirmation_timed_out"
    assert version.deploy_failure_reason == "agent_image_fleet_not_yet_serving"

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
    assert artifact.serving_status == "confirmation_timed_out"
    assert artifact.reason_code == "agent_image_fleet_not_yet_serving"
    # The fleet was consulted (twice: once per sweep) and never reported the
    # promoted SHA — the timeout came from the deadline branch, not a spurious
    # confirmation.
    assert fleet.await_count == 2


@pytest.mark.agent_image_gate
@pytest.mark.asyncio
async def test_agent_image_indeterminate_paths_emit_structured_log(
    db_session, mocker, caplog
):
    """The three indeterminate return paths each emit a structured log message
    identifying path=base_sha_unobservable, path=rebake_lookup_indeterminate,
    or path=fleet_not_yet_serving, plus version_id and promoted_sha."""
    import logging

    mocker.patch(
        "app.release_promotion._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )

    promoted_at = _utcnow()

    async def _run_check(
        version, rebake_return, fleet_return=False, *, no_deploy_state=False
    ):
        mocker.patch(
            "app.github_refs.agent_image_rebake_expected",
            new=AsyncMock(return_value=rebake_return),
        )
        mocker.patch(
            "app.compute.fleet_serving_source_sha",
            new=AsyncMock(return_value=fleet_return),
        )
        if not no_deploy_state:
            await _set_deploy_state(
                db_session,
                status=DeploymentStatus.STABLE,
                current_sha=_SHA_A,
                prior_deploy_sha=_SHA_B,
                deployed_at=promoted_at + timedelta(minutes=5),
            )
        await _confirm_serving_or_fail(
            version.id,
            session_factory=_shared_session_factory(db_session),
            now=promoted_at + timedelta(minutes=20),
        )

    deadline = promoted_at + timedelta(hours=1)

    # --- path: base_sha_unobservable (no deploy state → no base_sha) ---
    _project1, version1 = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    with caplog.at_level(logging.INFO, logger="app.release_artifacts"):
        await _run_check(version1, rebake_return=True, no_deploy_state=True)
    assert any(
        "base_sha_unobservable" in r.message
        and str(version1.id) in r.message
        and version1.promoted_sha in r.message
        for r in caplog.records
    ), f"Expected base_sha_unobservable log; got: {[r.message for r in caplog.records]}"

    caplog.clear()

    # --- path: rebake_lookup_indeterminate (rebake returns None) ---
    _project2, version2 = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    with caplog.at_level(logging.INFO, logger="app.release_artifacts"):
        await _run_check(version2, rebake_return=None)
    assert any(
        "rebake_lookup_indeterminate" in r.message
        and str(version2.id) in r.message
        and version2.promoted_sha in r.message
        for r in caplog.records
    ), (
        f"Expected rebake_lookup_indeterminate log; got: {[r.message for r in caplog.records]}"
    )

    caplog.clear()

    # --- path: fleet_not_yet_serving (rebake=True but fleet returns False) ---
    _project3, version3 = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )
    with caplog.at_level(logging.INFO, logger="app.release_artifacts"):
        await _run_check(version3, rebake_return=True, fleet_return=False)
    assert any(
        "fleet_not_yet_serving" in r.message
        and str(version3.id) in r.message
        and version3.promoted_sha in r.message
        for r in caplog.records
    ), f"Expected fleet_not_yet_serving log; got: {[r.message for r in caplog.records]}"


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
    assert version.serving_status not in ("unconfirmed",), (
        f"version still unconfirmed: {version.serving_status}"
    )

    # The backend_revision artifact row must be resolved.
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
                    ReleaseDeployArtifact.artifact_kind
                    == BACKEND_REVISION_ARTIFACT_KIND,
                )
            )
        )
        .scalars()
        .first()
    )
    assert artifact is not None
    assert artifact_is_unconfirmed(artifact)


# ===========================================================================
# Out-of-band supersession reconciliation:
# _reconcile_if_pending_qualification_superseded
#
# Covers the v10 force-promotion incident: a version is open+pending with NO
# promotion_pending sub-key (the qualifier never reached the promotion phase),
# but the candidate sha is already reachable from the promotion target — an
# operator force-merged it directly.  The reconciler detects this and stamps
# not_promoted/qualification_superseded promptly, preserving candidate_sha and
# audit findings so the re-qualify steering and finding ledger can act on them.
# ===========================================================================


@pytest.mark.asyncio
async def test_pending_qualification_superseded_out_of_band_reconciles(
    db_session, mocker
):
    """_reconcile_if_pending_qualification_superseded: a pending qualification
    with no promotion_pending whose candidate is already on the promotion target
    reconciles with blocker='qualification_superseded' and returns the version
    to stabilize (the v10 force-promotion incident class)."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = "beef" * 10
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "confidence": 0.82,
        "audit_findings": [{"severity": "high", "summary": "stale dependency"}],
        "started_at": _utcnow().isoformat(),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_qualification_superseded(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    is_ancestor.assert_awaited_once()
    # Ancestry check was against the promotion target, not the integration branch.
    assert is_ancestor.await_args.args[4] == "release"
    await db_session.refresh(version)
    # Version stays open — superseded does NOT auto-promote.
    assert version.status == "open"
    assert version.phase == "stabilize"
    qual = version.last_qualification
    assert qual["status"] == "not_promoted"
    assert qual["blocker"] == "qualification_superseded"
    # candidate_sha and audit_findings must be preserved for the finding ledger.
    assert qual["candidate_sha"] == candidate_sha
    assert qual["confidence"] == pytest.approx(0.82)
    assert qual["audit_findings"] == [
        {"severity": "high", "summary": "stale dependency"}
    ]
    assert "finalized_at" in qual


@pytest.mark.asyncio
async def test_reconcile_pending_superseded_skips_when_candidate_not_on_main(
    db_session, mocker
):
    """When the candidate is NOT yet reachable from the promotion target,
    the superseded reconciler is a no-op — the qualification legitimately
    continues until a real terminal signal arrives."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=False)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "started_at": _utcnow().isoformat(),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_qualification_superseded(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_awaited_once()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_reconcile_pending_superseded_skips_on_single_branch(db_session, mocker):
    """On a single-branch repo (integration_branch == promotion_target) the
    superseded reconciler is inert: no GitHub I/O and the version is untouched.
    _candidate_already_on_main would always return True for every pending
    qualification on such a repo, so bailing out is mandatory."""
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="main", promotion_target="main"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "started_at": _utcnow().isoformat(),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_qualification_superseded(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_reconcile_pending_superseded_skips_with_promotion_pending(
    db_session, mocker
):
    """A pending qualification WITH a promotion_pending sub-key is NOT handled
    by the superseded reconciler — that path belongs to the managed promotion
    reconciler and the promotion-pending timeout."""
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "pr_number": 77,
            "pr_url": "https://github.com/acme/widget/pull/77",
            "managed_check_wait": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_qualification_superseded(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert "promotion_pending" in version.last_qualification


@pytest.mark.asyncio
async def test_reconcile_pending_superseded_skips_when_not_pending(db_session, mocker):
    """The superseded reconciler gates on status=='pending' — a version already
    in any other qualification state (not_promoted, promotion_failed, etc.) is
    left entirely untouched."""
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "qualification_timeout",
        "candidate_sha": "abcd" * 10,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_qualification_superseded(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["blocker"] == "qualification_timeout"


@pytest.mark.asyncio
async def test_reconcile_pending_superseded_credential_failure_leaves_untouched(
    db_session, mocker
):
    """Fail-closed: when GitHub credential resolution fails, the superseded
    reconciler returns False and the version is left untouched for the normal
    qualification_timeout backstop — we never fabricate a supersession verdict
    based on an unverifiable ancestry probe."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(side_effect=RuntimeError("github 500")),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = "abcd" * 10
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "started_at": _utcnow().isoformat(),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_pending_qualification_superseded(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    is_ancestor.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["candidate_sha"] == candidate_sha


# ===========================================================================
# managed_check_wait reconciler — all persisted promotion PRs wait here for CI,
# then re-validate the exact target and retry the merge.
# ===========================================================================


def _managed_check_wait_qual(
    candidate_sha: str,
    *,
    pr_number: int = 42,
    pr_url: str = "https://github.com/acme/widget/pull/42",
    confidence: float = 0.95,
) -> dict:
    """Build a last_qualification dict with managed_check_wait=True."""
    return {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "confidence": confidence,
        "promotion_pending": {
            "pr_number": pr_number,
            "pr_url": pr_url,
            "managed_check_wait": True,
            "enabled_at": _utcnow().isoformat(),
        },
    }


def test_legacy_promotion_pending_normalizer_is_narrow_and_canonical():
    legacy = {
        "pr_number": 42,
        "pr_url": "https://github.com/acme/widget/pull/42",
        "enabled_at": _utcnow().isoformat(),
        **{key: "legacy" for key in _LEGACY_NATIVE_PROMOTION_PENDING_KEYS},
    }

    normalized, changed = _normalize_legacy_promotion_pending(legacy)

    assert changed is True
    assert normalized is not None
    assert normalized["managed_check_wait"] is True
    assert set(normalized).isdisjoint(_LEGACY_NATIVE_PROMOTION_PENDING_KEYS)
    assert set(legacy).issuperset(_LEGACY_NATIVE_PROMOTION_PENDING_KEYS)

    # Bare claims are a different state: compatibility must not invent a PR.
    assert _normalize_legacy_promotion_pending({"claiming": True}) == (None, False)


@pytest.mark.asyncio
async def test_managed_check_wait_green_revalidates_and_merges(db_session, mocker):
    """Core acceptance test: a managed_check_wait version with green CI and a
    passing Alembic guard triggers a real merge attempt; on success the version
    is recorded as promoted through the existing promotion success path.

    This exercises the full production path:
      promote() sets managed_check_wait=True → the reconciler picks it up, sees green
      CI, re-runs the Alembic guard (passes), retries the merge (succeeds),
      and drives the version to status="promoted".
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    # Candidate is NOT yet on main (the normal pending path).
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    candidate_sha = "abcd" * 10
    # PR is open with the exact qualified head SHA.
    pr_head = candidate_sha
    get_pr_mock = mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # CI is green on the PR head.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "details": []}
        ),
    )
    # Alembic guard passes (PR does not touch migration files in this mock path).
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    # Merge succeeds and returns the exact merge receipt.
    promoted_sha = "merge00" * 5
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "sha": promoted_sha}),
    )
    lock_mock = mocker.patch(
        "app.task_locking.acquire_merge_submission_locks",
        new=AsyncMock(return_value=None),
    )
    calls = mocker.Mock()
    calls.attach_mock(get_pr_mock, "get_pr")
    calls.attach_mock(lock_mock, "lock")
    calls.attach_mock(merge_mock, "merge")
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=promoted_sha),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.status == "promoted", (
        f"Expected promoted, got {version.status!r} "
        f"(last_qualification={version.last_qualification!r})"
    )
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification["confidence"] == pytest.approx(0.95)
    assert version.promoted_sha == promoted_sha
    assert merge_mock.call_args.kwargs["expected_head_sha"] == pr_head
    lock_mock.assert_awaited_once_with(
        db_session,
        project_id=version.project_id,
        repo_identity="acme/widget",
        base_branch="release",
        pr_number=42,
    )
    assert [call[0] for call in calls.mock_calls][-3:] == ["lock", "get_pr", "merge"]


@pytest.mark.asyncio
async def test_managed_check_wait_rejects_head_not_equal_to_qualified_candidate(
    db_session, mocker
):
    """An out-of-band release-branch push can never bypass qualification."""

    candidate_sha = "a" * 40
    wrong_head_sha = "b" * 40
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_branch_sha", new=AsyncMock(return_value="c" * 40)
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": wrong_head_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    checks = mocker.patch("app.github.get_check_runs_cached", new=AsyncMock())
    merge = mocker.patch("app.github.merge_pull_request", new=AsyncMock())

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    checks.assert_not_awaited()
    merge.assert_not_awaited()
    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending["check_snapshot"]["reason"] == "qualified_candidate_mismatch"
    assert version.last_qualification["candidate_sha"] == candidate_sha


@pytest.mark.parametrize("missing", ["head_ref", "head_sha", "base_ref"])
@pytest.mark.asyncio
async def test_managed_check_wait_rejects_incomplete_pr_identity(
    db_session, mocker, missing
):
    """Missing exact head/base identity is authority absence, never permission."""

    candidate_sha = "a" * 40
    pr_data = {
        "state": "open",
        "merged": False,
        "head": {"ref": "release-v1", "sha": candidate_sha},
        "base": {"ref": "release", "sha": "base0001"},
    }
    if missing == "head_ref":
        pr_data["head"].pop("ref")
    elif missing == "head_sha":
        pr_data["head"].pop("sha")
    else:
        pr_data["base"].pop("ref")

    mocker.patch(
        "app.github.get_repo_installation_token", new=AsyncMock(return_value="tok")
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value="c" * 40))
    mocker.patch("app.github.get_pull_request", new=AsyncMock(return_value=pr_data))
    checks = mocker.patch("app.github.get_check_runs_cached", new=AsyncMock())
    merge = mocker.patch("app.github.merge_pull_request", new=AsyncMock())

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    checks.assert_not_awaited()
    merge.assert_not_awaited()
    await db_session.refresh(version)
    assert (
        version.last_qualification["promotion_pending"]["check_snapshot"]["reason"]
        == "qualified_candidate_mismatch"
    )


@pytest.mark.asyncio
async def test_managed_check_wait_authoritative_merged_pr_records_exact_receipt(
    db_session, mocker
):
    candidate_sha = "a" * 40
    merge_sha = "d" * 40
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )

    async def reachable(_token, _owner, _repo, ancestor, _target, **_kwargs):
        return ancestor == merge_sha

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=reachable))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 42,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": merge_sha,
                "html_url": "https://github.com/acme/widget/pull/42",
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    migration_validation = mocker.patch(
        "app.release_reconciliation._validate_migration_chain_post_merge",
        new=AsyncMock(return_value=("ok", None)),
    )
    merge = mocker.patch("app.github.merge_pull_request", new=AsyncMock())
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    merge.assert_not_awaited()
    migration_validation.assert_awaited_once()
    assert migration_validation.call_args.args[4] == merge_sha
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_managed_check_wait_merged_wrong_head_is_stop_level_incident(
    db_session, mocker
):
    candidate_sha = "a" * 40
    wrong_head_sha = "b" * 40
    merge_sha = "d" * 40
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_branch_sha", new=AsyncMock(return_value="c" * 40)
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 42,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": merge_sha,
                "html_url": "https://github.com/acme/widget/pull/42",
                "head": {"ref": "release-v1", "sha": wrong_head_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    merge = mocker.patch("app.github.merge_pull_request", new=AsyncMock())

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    merge.assert_not_awaited()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_integrity_violation"
    assert version.last_qualification["automatic_redrive_blocked"] is True
    evidence = version.last_qualification["integrity_evidence"]
    assert evidence["expected_candidate_sha"] == candidate_sha
    assert evidence["observed_head_sha"] == wrong_head_sha


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_side", ["head", "base"])
async def test_managed_check_wait_revalidates_exact_target_after_checks(
    db_session, mocker, changed_side
):
    """A base or head move during CI/guard invalidates the evidence and defers."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value="target-tip"))
    candidate_sha = "head-old"
    initial = {
        "state": "open",
        "merged": False,
        "head": {"ref": "release-v1", "sha": "head-old"},
        "base": {"ref": "release", "sha": "base-old"},
    }
    fresh = {
        "state": "open",
        "merged": False,
        "head": {
            "ref": "release-v1",
            "sha": "head-new" if changed_side == "head" else "head-old",
        },
        "base": {
            "ref": "release",
            "sha": "base-new" if changed_side == "base" else "base-old",
        },
    }
    mocker.patch(
        "app.github.get_pull_request",
        # Exact-receipt preflight reads once; the managed pass captures the same
        # pair, then the post-evidence re-read observes the movement.
        new=AsyncMock(side_effect=[initial, initial, fresh]),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "details": []}
        ),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    merge_mock.assert_not_awaited()
    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending["managed_check_wait"] is True
    assert pending["check_snapshot"]["reason"] == "pr_target_changed_after_checks"


@pytest.mark.asyncio
async def test_managed_check_wait_ci_red_reports_cancelled_and_action_required_names(
    db_session, mocker
):
    """When the PR's CI is red, the reconciler records required_check_failed with
    the failing check names so retry machinery applies.

    The red is backed only by infrastructure conclusions — ``cancelled`` /
    ``timed_out`` shards and an ``action_required`` human gate — with ZERO
    genuinely failing tests.  The reconciler still tears the PR down (CI is
    definitively red), but the promotion-CI harvest must record the failure as
    infrastructure and skip spawning a code-fix task that has nothing to
    remediate (``_handle_main_branch_ci_failure`` must not be invoked)."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "badc0de0" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # Every non-green conclusion is infrastructure or a human gate — NO check has
    # a genuine `failure` test, so the harvest must not spawn a fix.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {
                        "name": "backend",
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                    {
                        "name": "deploy",
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                    {
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "timed_out",
                    },
                    {
                        "name": "security",
                        "status": "completed",
                        "conclusion": "action_required",
                    },
                    {
                        "name": "frontend",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {"name": "docs", "status": "completed", "conclusion": "neutral"},
                    {"name": "smoke", "status": "completed", "conclusion": "skipped"},
                ],
            }
        ),
    )
    handle_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure",
        new=AsyncMock(),
    )
    refresh_mock = mocker.patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=True),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "required_check_failed"
    detail = version.last_qualification.get("detail", "")
    assert "backend" in detail
    assert "deploy" in detail
    assert "lint" in detail
    assert "security" in detail
    assert "frontend" not in detail
    assert "docs" not in detail
    assert "smoke" not in detail
    assert "promotion_pending" not in version.last_qualification
    # Zero genuinely failing tests → the harvest records the failure as
    # infrastructure (Strategic refresh armed) and spawns NO code-fix task.
    refresh_mock.assert_awaited_once()
    handle_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_check_wait_transient_cancellation_left_pending(
    db_session, mocker
):
    """Core acceptance test: an aggregate gate that launders a cancelled matrix
    shard must NOT tear down the promotion PR.

    A ``CI / Backend`` aggregate job reports conclusion ``failure`` whenever ONE
    backend shard is infrastructure-cancelled, even though no shard genuinely
    failed — the confirmed v28 attempt-6 promotion failure on PR #16885, where
    the promotion died on ``CI / Backend`` with zero failing tests.  The
    reconciler's genuine-failure guard must recognize the laundering (via the
    aggregate-parent-aware ``_details_have_genuine_failure`` / the boundary
    ``genuine_failure`` signal) and leave the version pending so the candidate
    can recover when CI re-runs.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "c0ffee00" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # The aggregate gate reports `failure` only because one shard was cancelled /
    # timed out — `genuine_failure` is False (the signal the get_check_runs
    # boundary computes from these same details in production).
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "genuine_failure": False,
                "details": [
                    {
                        "name": "CI / Backend",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                    {
                        "name": "CI / Backend (shard 1)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "CI / Backend (shard 2)",
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                    {
                        "name": "CI / Backend (shard 3)",
                        "status": "completed",
                        "conclusion": "timed_out",
                    },
                ],
            }
        ),
    )
    # Cap at 5 (default); first sweep should leave pending, not tear down.
    mocker.patch(
        "app.release_reconciliation._max_transient_ci_red_sweeps",
        return_value=5,
    )
    # Cleanup / terminal path must NOT be called.
    cleanup = mocker.patch(
        "app.release_reconciliation._cleanup_managed_check_wait_failure_artifacts",
        new=AsyncMock(),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False, (
        "A laundered aggregate gate (cancelled shard) must NOT drive the version "
        "to a terminal state"
    )
    cleanup.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    # Counter was incremented so subsequent sweeps can eventually escalate.
    pending = version.last_qualification["promotion_pending"]
    assert pending.get("transient_red_count") == 1
    assert pending.get("managed_check_wait") is True


@pytest.mark.asyncio
async def test_managed_check_wait_aggregate_gate_laundered_cancelled_shard_left_pending(
    db_session, mocker
):
    """The same laundered-aggregate scenario WITHOUT the boundary ``genuine_failure``
    field: the reconciler must still discount the aggregate gate by falling back
    to the details-based ``_details_have_genuine_failure`` (covers hand-built
    fixtures and callers that pre-date the field)."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "d00dbeef" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # No `genuine_failure` key — the details-only fallback must apply the same
    # aggregate-parent discount.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {
                        "name": "CI / Backend",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                    {
                        "name": "CI / Backend (shard 1)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "CI / Backend (shard 2)",
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                ],
            }
        ),
    )
    mocker.patch(
        "app.release_reconciliation._max_transient_ci_red_sweeps",
        return_value=5,
    )
    cleanup = mocker.patch(
        "app.release_reconciliation._cleanup_managed_check_wait_failure_artifacts",
        new=AsyncMock(),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(pr_head)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    cleanup.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    pending = version.last_qualification["promotion_pending"]
    assert pending.get("transient_red_count") == 1


@pytest.mark.asyncio
async def test_managed_check_wait_aggregate_gate_with_genuine_shard_records_required_check_failed(
    db_session, mocker
):
    """When a matrix CHILD genuinely fails, the aggregate parent's ``failure`` is
    not laundered — the reconciler must still tear down and record
    ``required_check_failed`` (a real failing test is not transient)."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "bad5c0de" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # Shard 4 genuinely failed (e.g. a real test failure) — the gate failure is
    # backed by a genuine child, so this is a real red CI.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {
                        "name": "CI / Backend",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                    {
                        "name": "CI / Backend (shard 1)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "CI / Backend (shard 2)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "CI / Backend (shard 4)",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                ],
            }
        ),
    )
    mocker.patch(
        "app.release_reconciliation._max_transient_ci_red_sweeps",
        return_value=5,
    )
    cleanup = mocker.patch(
        "app.release_reconciliation._cleanup_managed_check_wait_failure_artifacts",
        new=AsyncMock(return_value=True),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(pr_head)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    cleanup.assert_called_once()
    await db_session.refresh(version)
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "required_check_failed"
    detail = version.last_qualification.get("detail", "")
    assert "CI / Backend (shard 4)" in detail
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_managed_check_wait_timed_out_only_leaves_pending(db_session, mocker):
    """A check result whose only non-green conclusion is timed_out (no failure /
    action_required) must leave the version pending — same guard as the cancelled
    path."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "dead0000" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {
                        "name": "ci/infra",
                        "status": "completed",
                        "conclusion": "timed_out",
                    },
                ],
            }
        ),
    )
    mocker.patch(
        "app.release_reconciliation._max_transient_ci_red_sweeps",
        return_value=5,
    )
    cleanup = mocker.patch(
        "app.release_reconciliation._cleanup_managed_check_wait_failure_artifacts",
        new=AsyncMock(),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    cleanup.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_managed_check_wait_transient_red_escalates_after_cap(db_session, mocker):
    """Once the transient-red sweep counter reaches the configured cap the
    reconciler MUST escalate to the terminal ``required_check_failed`` path so a
    genuinely stuck check does not pin the version open forever."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "f00df00d" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {
                        "name": "ci/backend-shard-4",
                        "status": "completed",
                        "conclusion": "cancelled",
                    },
                ],
            }
        ),
    )
    # Set cap to 3; seed the counter at 2 so this sweep is the cap sweep.
    mocker.patch(
        "app.release_reconciliation._max_transient_ci_red_sweeps",
        return_value=3,
    )
    mocker.patch(
        "app.release_reconciliation._cleanup_managed_check_wait_failure_artifacts",
        new=AsyncMock(),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    qual = _managed_check_wait_qual(candidate_sha)
    # Pre-seed counter so we're at cap - 1 sweeps already done.
    qual["promotion_pending"]["transient_red_count"] = 2
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True, (
        "After reaching the transient-red sweep cap the version must be driven "
        "to a terminal state"
    )
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "required_check_failed"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_managed_check_wait_transient_red_count_resets_on_pending(
    db_session, mocker
):
    """transient_red_count must be reset when CI leaves the red state in the
    managed_check_wait reconciler (via _persist_managed_check_wait_snapshot)."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "cafebabe" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # CI is pending (in_progress) — should reset counter.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "in_progress",
                "conclusion": None,
                "details": [
                    {
                        "name": "ci/backend",
                        "status": "in_progress",
                        "conclusion": None,
                    },
                ],
            }
        ),
    )
    mocker.patch(
        "app.release_reconciliation._max_transient_ci_red_sweeps",
        return_value=5,
    )
    mocker.patch(
        "app.release_reconciliation._cleanup_managed_check_wait_failure_artifacts",
        new=AsyncMock(),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    qual = _managed_check_wait_qual(candidate_sha)
    qual["promotion_pending"]["transient_red_count"] = 1
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    pending = version.last_qualification["promotion_pending"]
    # Counter must be cleared so the next transient-red sweep starts fresh at 1.
    assert not pending.get("transient_red_count")


@pytest.mark.asyncio
async def test_managed_check_wait_defers_on_migration_metadata_unfetchable(
    db_session, mocker
):
    """When CI is green but the migration metadata cannot be fetched, the guard
    verdict is ``defer`` — the reconciler must leave the version PENDING for the
    next sweep, NOT record a terminal ``migration_guard_blocked`` failure.

    A large migration-carrying rollup whose diff momentarily 406s would
    otherwise be bounced to stabilize on a transient metadata fetch error; the
    3-way verdict keeps it in managed_check_wait until the metadata is readable.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "feedface" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # CI is green — reaches the pre-merge Alembic guard.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "details": []}
        ),
    )
    # The PR touches a migration file, but the diff fetch fails → guard "defer".
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0042_x.py"}]
        ),
    )
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(side_effect=RuntimeError("502 from GitHub")),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # Deferred — not terminal. The version stays pending for the next sweep and
    # the merge never ran.
    assert reconciled is False
    merge_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["promotion_pending"]["managed_check_wait"] is True
    assert "error" not in version.last_qualification


@pytest.mark.asyncio
async def test_managed_check_wait_out_of_band_landing_records_promoted(
    db_session, mocker
):
    """When the candidate is already on the promotion target (out-of-band merge),
    the reconciler records success through the existing promotion success path."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    candidate_sha = "abcd" * 10
    promoted_sha = "release0" * 5
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 42,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": promoted_sha,
                "head": {"ref": "release-v1", "sha": candidate_sha},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=promoted_sha))
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = _managed_check_wait_qual(
        candidate_sha, confidence=0.88
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.last_qualification["confidence"] == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_managed_check_wait_alembic_guard_blocks(db_session, mocker):
    """When CI is green but the Alembic pre-merge guard detects a dual-head
    collision, the reconciler records migration_guard_blocked."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "deadbeef" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "details": []}
        ),
    )
    # PR touches a migration file — Alembic guard will fire.
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[{"filename": "backend/alembic/versions/0200_new_col.py"}]
        ),
    )
    # Target branch has a duplicate revision slot — block.
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(
            return_value=[
                "0199_prev.py",
                "0200_new_col.py",
                "0200_other_col.py",  # genuine third-party duplicate
            ]
        ),
    )
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=_file_text_from_map({"0199": None, "0200": "0199"})),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "migration_guard_blocked"
    assert "promotion_pending" not in version.last_qualification


@pytest.mark.asyncio
async def test_managed_check_wait_alembic_defer_leaves_pending(db_session, mocker):
    """When CI is green but the Alembic pre-merge guard returns 'defer' (metadata
    temporarily unavailable), the reconciler leaves the version pending for the
    next sweep — NO terminal failure, NO artifact cleanup.

    This mirrors the out-of-band post-merge site already in this function which
    treats 'defer' as keep-waiting/return False.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "d3ferb33f" * 4 + "dddd"
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "details": []}
        ),
    )
    close_pr_mock = mocker.patch(
        "app.github.close_pull_request", new=AsyncMock(return_value=True)
    )
    delete_branch_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request", new=AsyncMock(return_value={"merged": True})
    )
    # Guard returns defer — metadata temporarily unavailable (not a hard block).
    mocker.patch(
        "app.rollup_migration_validation._validate_rollup_migration_chain",
        new=AsyncMock(
            return_value=("defer", "could not list PR files: 503 Service Unavailable")
        ),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # Defer → keep waiting; returns False (not reconciled to a terminal state).
    assert reconciled is False
    # Merge never ran; no cleanup.
    merge_mock.assert_not_called()
    close_pr_mock.assert_not_called()
    delete_branch_mock.assert_not_called()
    # Version stays open and pending — no terminal record written.
    await db_session.refresh(version)
    assert version.status == "open"
    qual = version.last_qualification
    assert qual["status"] == "pending"
    assert "error" not in qual
    assert qual.get("promotion_pending", {}).get("managed_check_wait") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "check_result, guard_result, merge_result, expected_error, expected_detail",
    [
        (
            {
                "status": "completed",
                "conclusion": "failure",
                "details": [
                    {"name": "backend", "status": "completed", "conclusion": "failure"},
                    {
                        "name": "frontend",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ],
            },
            None,
            None,
            "required_check_failed",
            "backend",
        ),
        (
            {"status": "completed", "conclusion": "success", "details": []},
            ("block", "duplicate migration head"),
            None,
            "migration_guard_blocked",
            "duplicate migration head",
        ),
        (
            {"status": "completed", "conclusion": "success", "details": []},
            ("ok", None),
            {"merged": False, "conflict": True, "message": "merge conflict"},
            "merge_blocked",
            "merge conflict",
        ),
        (
            {"status": "completed", "conclusion": "success", "details": []},
            ("ok", None),
            {
                "merged": False,
                "conflict": False,
                "message": "required reviews not satisfied",
            },
            "protection_blocked_review",
            "required reviews not satisfied",
        ),
    ],
    ids=[
        "required_check_failed",
        "migration_guard_blocked",
        "merge_blocked",
        "protection_blocked_review",
    ],
)
async def test_managed_check_wait_terminal_failure_closes_pr_and_deletes_branch(
    db_session,
    mocker,
    check_result,
    guard_result,
    merge_result,
    expected_error,
    expected_detail,
):
    """Terminal managed_check_wait failures close the PR and delete the branch."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": "feedface" * 5},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value=check_result),
    )
    close_pull_request = mocker.patch(
        "app.github.close_pull_request", new=AsyncMock(return_value=True)
    )
    delete_branch = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    if guard_result is not None:
        mocker.patch(
            "app.rollup_migration_validation._validate_rollup_migration_chain",
            new=AsyncMock(return_value=guard_result),
        )
    if merge_result is not None:
        mocker.patch(
            "app.github.merge_pull_request",
            new=AsyncMock(return_value=merge_result),
        )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = "feedface" * 5
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == expected_error
    assert expected_detail in version.last_qualification.get("detail", "")
    assert "promotion_pending" not in version.last_qualification
    assert close_pull_request.await_count == 1
    assert delete_branch.await_count == 1


@pytest.mark.asyncio
async def test_managed_check_wait_transient_merge_refusal_ci_flipped_leaves_pending(
    db_session, mocker
):
    """A non-conflict merge refusal whose CI re-check is NOT green leaves the
    version pending (transient race: check re-queued between classification and
    merge call).  No PR close or branch delete should occur."""
    green_check = {"status": "completed", "conclusion": "success", "details": []}
    pending_check = {
        "status": "in_progress",
        "conclusion": None,
        "details": [
            {"name": "backend", "status": "in_progress", "conclusion": None},
        ],
    }
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": "feedface" * 5},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # First call (initial CI check) returns green; second call (re-check after
    # merge refusal) returns pending — simulating a transient check flip.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(side_effect=[green_check, pending_check]),
    )
    mocker.patch(
        "app.rollup_migration_validation._validate_rollup_migration_chain",
        new=AsyncMock(return_value=("ok", None)),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": False,
                "message": "required reviews not satisfied",
            }
        ),
    )
    close_pull_request = mocker.patch(
        "app.github.close_pull_request", new=AsyncMock(return_value=True)
    )
    delete_branch = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = "feedface" * 5
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # Transient: leave pending, no terminal failure recorded.
    assert reconciled is False
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert "error" not in version.last_qualification
    assert (
        version.last_qualification.get("promotion_pending", {}).get(
            "managed_check_wait"
        )
        is True
    )
    # PR must NOT be closed or branch deleted on a transient refusal.
    assert close_pull_request.await_count == 0
    assert delete_branch.await_count == 0


@pytest.mark.asyncio
async def test_managed_check_wait_transient_merge_refusal_ci_recheck_fails_leaves_pending(
    db_session, mocker
):
    """A non-conflict merge refusal where the CI re-check fetch raises an
    exception is treated as transient (fail-open): the version stays pending,
    no failure is recorded, and no PR/branch cleanup is triggered."""
    green_check = {"status": "completed", "conclusion": "success", "details": []}
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": "feedface" * 5},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # First call returns green; second call (re-check) raises a transient error.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(side_effect=[green_check, RuntimeError("network timeout")]),
    )
    mocker.patch(
        "app.rollup_migration_validation._validate_rollup_migration_chain",
        new=AsyncMock(return_value=("ok", None)),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": False,
                "message": "required reviews not satisfied",
            }
        ),
    )
    close_pull_request = mocker.patch(
        "app.github.close_pull_request", new=AsyncMock(return_value=True)
    )
    delete_branch = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = "feedface" * 5
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # Fail-open: leave pending, no terminal failure.
    assert reconciled is False
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert "error" not in version.last_qualification
    assert (
        version.last_qualification.get("promotion_pending", {}).get(
            "managed_check_wait"
        )
        is True
    )
    assert close_pull_request.await_count == 0
    assert delete_branch.await_count == 0


@pytest.mark.asyncio
async def test_managed_check_wait_still_pending_ci_persists_check_snapshot(
    db_session, mocker
):
    """When CI is still pending, the reconciler leaves the version in its
    managed_check_wait pending state and persists bounded CI telemetry."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "cafebabe" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # CI still in progress, with more check details than the persisted snapshot
    # should retain.
    details = [
        {"name": "backend", "status": "in_progress", "conclusion": None},
        {"name": "frontend", "status": "queued", "conclusion": None},
        *[
            {"name": f"extra-{i}", "status": "queued", "conclusion": None}
            for i in range(12)
        ],
    ]
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "in_progress",
                "conclusion": None,
                "details": details,
            }
        ),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    pending = version.last_qualification["promotion_pending"]
    assert pending["managed_check_wait"] is True
    assert pending["pr_number"] == 42
    assert pending["pr_url"] == "https://github.com/acme/widget/pull/42"
    assert pending["ci_state"] == "pending"
    assert pending["ci_classified_at"] == pending["check_snapshot"]["classified_at"]
    snapshot = pending["check_snapshot"]
    assert snapshot["state"] == "pending"
    assert snapshot["head_sha"] == pr_head
    assert snapshot["total_checks"] == len(details)
    assert snapshot["truncated"] is True
    assert snapshot["checks"][:2] == [
        {"name": "backend", "status": "in_progress"},
        {"name": "frontend", "status": "queued"},
    ]
    assert len(snapshot["checks"]) == 10


@pytest.mark.asyncio
async def test_managed_check_wait_no_checks_persists_snapshot_idempotently(
    db_session, mocker
):
    """No-checks CI is non-terminal but still durable operator telemetry.

    Repeated sweeps overwrite the bounded snapshot without clobbering the
    promotion PR provenance the retry path needs.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head_first = "1111aaaa" * 5
    pr_head_second = pr_head_first
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            side_effect=[
                {
                    "state": "open",
                    "merged": False,
                    "head": {"ref": "release-v1", "sha": pr_head_first},
                    "base": {"ref": "release", "sha": "base0001"},
                },
                {
                    "state": "open",
                    "merged": False,
                    "head": {"ref": "release-v1", "sha": pr_head_second},
                    "base": {"ref": "release", "sha": "base0001"},
                },
            ]
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "no_checks",
                "conclusion": None,
                "details": [],
            }
        ),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head_first
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    first = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    second = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert first is False
    assert second is False
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    pending = version.last_qualification["promotion_pending"]
    assert pending["managed_check_wait"] is True
    assert pending["pr_number"] == 42
    assert pending["pr_url"] == "https://github.com/acme/widget/pull/42"
    assert set(pending).isdisjoint(_LEGACY_NATIVE_PROMOTION_PENDING_KEYS)
    assert pending["ci_state"] == "no_checks"
    snapshot = pending["check_snapshot"]
    assert snapshot["state"] == "no_checks"
    assert snapshot["head_sha"] == pr_head_second
    assert snapshot["total_checks"] == 0
    assert snapshot["checks"] == []
    assert snapshot["truncated"] is False


@pytest.mark.asyncio
async def test_managed_check_wait_no_checks_on_required_promotion_target_blocks(
    db_session, mocker
):
    """A relaxed integration branch must not make promotion-target no_checks pass."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "cafe1234" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "main", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "no_checks",
                "conclusion": None,
                "details": [],
            }
        ),
    )
    merge_mock = mocker.patch("app.github.merge_pull_request", new=AsyncMock())

    project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="main"
    )
    project.build_config = {
        "requires_ci_checks": False,
        "has_ci": True,
        "ci_required_branches": ["main"],
    }
    db_session.add(project)
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    merge_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    pending = version.last_qualification["promotion_pending"]
    assert pending["managed_check_wait"] is True
    assert pending["ci_state"] == "no_checks"
    assert pending["check_snapshot"]["state"] == "no_checks"


@pytest.mark.asyncio
async def test_managed_check_wait_no_checks_no_ci_repo_proceeds_to_merge(
    db_session, mocker
):
    """A managed_check_wait version on a no-CI repo with no_checks CI proceeds
    to a merge attempt instead of persisting a snapshot and returning False.

    This exercises the fall-through added at release_reconciliation.py:1586:
    when ci_state == no_checks AND requires_ci_checks(project) is False, the
    reconciler skips the snapshot-and-return path and falls through to the
    Alembic guard + merge attempt.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "cafe1234" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "no_checks",
                "conclusion": None,
                "details": [],
            }
        ),
    )
    # Alembic guard passes (no migration files touched).
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    promoted_sha = "merge001" * 5
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "sha": promoted_sha}),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=promoted_sha),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    # Mark the project as no-CI so requires_ci_checks() returns False.
    project.build_config = {"requires_ci_checks": False}
    db_session.add(project)
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # The reconciler must proceed to merge (returns True) rather than persisting
    # a check_snapshot and returning False — which would strand the version.
    assert reconciled is True
    merge_mock.assert_called_once()
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification["status"] == "promoted"
    assert version.promoted_sha == promoted_sha


@pytest.mark.asyncio
async def test_managed_check_wait_snapshot_reloads_fresh_row_after_promotion(
    mocker,
):
    """A stale cached row must be expired before the helper re-reads it."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"sha": "cafebabe" * 5},
            }
        ),
    )
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value={}))
    # The reconciler harvests _auto_remediation via read_telemetry_or_doc; the
    # minimal _FakeSession below cannot model that select(ProjectTelemetry.value)
    # query (it returns a ReleaseVersion for every execute()). Stub the harvest
    # to the same empty-config value a real session yields when the project never
    # customized auto-remediation, so the test exercises the expire/re-read path.
    mocker.patch(
        "app.project_telemetry.read_telemetry_or_doc",
        new=AsyncMock(return_value={}),
    )

    candidate_sha = "abcd" * 10
    version_id = uuid4()
    project_id = uuid4()
    stale = rv.ReleaseVersion(project_id=project_id, id=version_id)
    stale.status = "open"
    stale.last_qualification = _managed_check_wait_qual(candidate_sha)
    fresh = rv.ReleaseVersion(project_id=project_id, id=version_id)
    fresh.status = "promoted"
    fresh.last_qualification = {
        "status": "promoted",
        "candidate_sha": candidate_sha,
        "confidence": 0.95,
        "promoted_sha": "feedface" * 5,
    }

    class _FakeResult:
        def __init__(self, value):
            self._value = value

        def scalars(self):
            return self

        def first(self):
            return self._value

    class _FakeSession:
        def __init__(self, stale_row, fresh_row):
            self._stale_row = stale_row
            self._fresh_row = fresh_row
            self.expired = False

        def expire_all(self):
            self.expired = True

        async def execute(self, _stmt):
            return _FakeResult(self._fresh_row if self.expired else self._stale_row)

        async def get(self, model, pk):
            if model is Project and pk == project_id:
                return Project(
                    id=project_id,
                    name="Reconcile-Test",
                    user_id="reconcile-user",
                    github_owner="acme",
                    github_repo="widget",
                    integration_branch="automain",
                    promotion_target="release",
                )
            return None

        def add(self, _obj):
            pass

        async def commit(self):
            pass

        async def rollback(self):
            pass

    fake_session = _FakeSession(stale, fresh)

    @contextlib.asynccontextmanager
    async def _factory():
        yield fake_session

    reconciled = await _reconcile_if_managed_check_wait(
        version_id, session_factory=_factory
    )

    assert reconciled is False
    assert fake_session.expired is True
    assert "check_snapshot" not in stale.last_qualification["promotion_pending"]
    assert fresh.status == "promoted"
    assert fresh.last_qualification["status"] == "promoted"


@pytest.mark.asyncio
async def test_managed_check_wait_timeout_backstop_still_applies(db_session, mocker):
    """The promotion_pending_timeout backstop applies to managed_check_wait
    versions as well — a stranded sub-state is force-failed after the cap."""
    _patch_timeout_closed_pr(mocker, pr_number=55)
    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    stranded_enabled = (
        _utcnow() - timedelta(seconds=_promotion_pending_timeout_seconds() + 3600)
    ).isoformat()
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "pr_number": 55,
            "pr_url": "https://github.com/acme/widget/pull/55",
            "managed_check_wait": True,
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


@pytest.mark.asyncio
async def test_managed_check_wait_timeout_closes_orphaned_pr(db_session, mocker):
    """Timeout persists cleanup debt, then clears it only after PR closure."""
    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    stranded_enabled = (
        _utcnow() - timedelta(seconds=_promotion_pending_timeout_seconds() + 3600)
    ).isoformat()
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "pr_number": 77,
            "pr_url": "https://github.com/acme/widget/pull/77",
            "managed_check_wait": True,
            "enabled_at": stranded_enabled,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.release_reconciliation._resolve_github_with_session",
        new=AsyncMock(return_value=("tok", "acme", "widget")),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 77,
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": "abcd" * 10},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    cleanup_mock = mocker.patch(
        "app.release_reconciliation._cleanup_managed_check_wait_failure_artifacts",
        new=AsyncMock(return_value=True),
    )

    timed_out = await _reconcile_if_promotion_pending_timed_out(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert timed_out is True
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "promotion_pending_timeout"
    assert "promotion_pending" not in version.last_qualification
    # The orphaned managed promotion PR was best-effort closed.
    cleanup_mock.assert_awaited_once()
    _args, _kwargs = cleanup_mock.await_args
    assert _args[3] == f"release-v{version.version_number}"  # release branch
    assert _args[5] == 77  # pr_number


# ===========================================================================
# managed_check_wait: 405 retriable vs terminal classification
# ===========================================================================


@pytest.mark.asyncio
async def test_managed_check_wait_405_retriable_vs_terminal(db_session, mocker):
    """A 405 merge refusal with a terminal message records merge_refused_405_terminal
    immediately; a retriable message leaves the version pending for the next sweep.

    Exercises _classify_405_message and the two new branches in
    _reconcile_if_managed_check_wait's merge-result else-block:
      - terminal message ("Pull request is closed") -> promotion_failed with
        error="merge_refused_405_terminal", version back to stabilize.
      - retriable message ("Required status check ... has not passed") -> version
        stays pending (reconciled=False), no promotion_failed recorded.
    """
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "deadbeef" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "details": []}
        ),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch("app.github.delete_branch", new=AsyncMock(return_value=True))
    mocker.patch("app.github.close_pull_request", new=AsyncMock(return_value={}))

    # --- Terminal 405: PR closed ---
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": False,
                "message": "Pull request is closed",
            }
        ),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    version.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is True, "terminal 405 should return reconciled=True"
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "merge_refused_405_terminal"
    assert "pull request is closed" in version.last_qualification.get(
        "detail", ""
    ).lower()

    # --- Retriable 405: "Required status check ... has not passed" ---
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": False,
                "message": "Required status check 'CI / Backend' has not passed",
            }
        ),
    )

    _project2, version2 = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version2.last_qualification = _managed_check_wait_qual(candidate_sha)
    db_session.add(version2)
    await db_session.commit()
    await db_session.refresh(version2)

    reconciled2 = await _reconcile_if_managed_check_wait(
        version2.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled2 is False, (
        "retriable 405 should return reconciled=False (leave pending)"
    )
    await db_session.refresh(version2)
    assert version2.status == "open"
    assert version2.last_qualification["status"] == "pending"
    assert "promotion_pending" in version2.last_qualification


# ===========================================================================
# API-error sweep tracking — consecutive _from_api_error results increment
# api_error_sweep_count and surface a warning at the threshold.
# ===========================================================================

from app.release_reconciliation import _API_ERROR_SWEEP_WARN_THRESHOLD  # noqa: E402


@pytest.mark.asyncio
async def test_managed_check_wait_api_error_sweep_count_increments(
    db_session, mocker, caplog
):
    """Consecutive _from_api_error check-run results increment api_error_sweep_count
    in the snapshot, and a WARNING-level log fires once count reaches the threshold."""
    import logging

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "deadbeef" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # get_check_runs_cached returns an api-error response (status=pending + _from_api_error=True)
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "pending",
                "conclusion": None,
                "_from_api_error": True,
                "details": [],
            }
        ),
    )

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    # Pre-seed with count just below the threshold so one more sweep crosses it.
    qual = _managed_check_wait_qual(candidate_sha)
    qual["promotion_pending"]["api_error_sweep_count"] = (
        _API_ERROR_SWEEP_WARN_THRESHOLD - 1
    )
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    with caplog.at_level(logging.WARNING, logger="app.release_reconciliation"):
        reconciled = await _reconcile_if_managed_check_wait(
            version.id, session_factory=_shared_session_factory(db_session)
        )

    assert reconciled is False
    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending.get("api_error_sweep_count") == _API_ERROR_SWEEP_WARN_THRESHOLD
    # The warning log must mention "api-error" so operators can distinguish it
    # from genuine in-progress CI.
    assert any(
        "api-error" in record.message.lower() or "api_error" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), "expected a WARNING log mentioning api-error sweeps"


@pytest.mark.asyncio
async def test_managed_check_wait_api_error_count_resets_on_real_check(
    db_session, mocker
):
    """A non-api-error response resets api_error_sweep_count to 0."""
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    pr_head = "cafebabe" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": pr_head},
                "base": {"ref": "release", "sha": "base0001"},
            }
        ),
    )
    # A real in-progress response (no _from_api_error flag).
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

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    candidate_sha = pr_head
    qual = _managed_check_wait_qual(candidate_sha)
    qual["promotion_pending"]["api_error_sweep_count"] = 5
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    reconciled = await _reconcile_if_managed_check_wait(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert reconciled is False
    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert not pending.get("api_error_sweep_count")


# ===========================================================================
# ci_unresolved restore-shape — rediscovered promotion PRs return to the sole
# managed_check_wait authority.
# ===========================================================================

from app.release_qualifier import _guard_ci_unresolved_promotion_pending  # noqa: E402


@pytest.mark.asyncio
async def test_ci_unresolved_guard_restores_managed_check_wait(
    db_session, mocker
):
    """A rediscovered open release PR is restored in canonical managed form."""
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value="target" * 7))
    mocker.patch(
        "app.github.get_open_pull_requests",
        new=AsyncMock(
            return_value=[
                {
                    "number": 101,
                    "html_url": "https://github.com/acme/widget/pull/101",
                    "state": "open",
                    "head": {"ref": "release-v1", "sha": "abcd" * 10},
                    "base": {"ref": "release", "sha": "base0001"},
                }
            ]
        ),
    )
    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "abcd" * 10,
        "blocker": "ci_unresolved",
        "confidence": 0.0,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    base_record = {
        "confidence": 0.0,
        "blocker": "ci_unresolved",
        "candidate_sha": "abcd" * 10,
    }

    result = await _guard_ci_unresolved_promotion_pending(
        session=db_session,
        version=version,
        candidate_sha="abcd" * 10,
        base_record=base_record,
        token="tok",
        owner="acme",
        repo="widget",
        project=_project,
    )

    assert result is not None
    assert result.get("managed_check_wait") is True

    # The guard leaves its restored promotion_pending write pending for the
    # caller's commit (in prod _finalize_locked_version commits after it);
    # commit here before refresh so the refresh reads the persisted state
    # instead of discarding the pending write under real ORM expiry.
    await db_session.commit()
    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending.get("managed_check_wait") is True
    assert set(pending).isdisjoint(_LEGACY_NATIVE_PROMOTION_PENDING_KEYS)
    assert pending.get("restored_by_ci_unresolved_guard") is True


@pytest.mark.asyncio
async def test_ci_unresolved_guard_restores_non_migration_pr_to_same_managed_wait(
    db_session, mocker
):
    """PR content does not fork promotion authority; every PR is managed."""
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value="target" * 7))
    mocker.patch(
        "app.github.get_open_pull_requests",
        new=AsyncMock(
            return_value=[
                {
                    "number": 202,
                    "html_url": "https://github.com/acme/widget/pull/202",
                    "state": "open",
                    "head": {"ref": "release-v1", "sha": "cafe" * 10},
                    "base": {"ref": "release", "sha": "base0001"},
                }
            ]
        ),
    )
    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="release"
    )
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "cafe" * 10,
        "blocker": "ci_unresolved",
        "confidence": 0.0,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    base_record = {
        "confidence": 0.0,
        "blocker": "ci_unresolved",
        "candidate_sha": "cafe" * 10,
    }

    result = await _guard_ci_unresolved_promotion_pending(
        session=db_session,
        version=version,
        candidate_sha="cafe" * 10,
        base_record=base_record,
        token="tok",
        owner="acme",
        repo="widget",
        project=_project,
    )

    assert result is not None
    assert result.get("managed_check_wait") is True

    # Commit before refresh — the guard's promotion_pending write rides the
    # caller's commit (see the migration-PR variant above).
    await db_session.commit()
    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending.get("managed_check_wait") is True
    assert set(pending).isdisjoint(_LEGACY_NATIVE_PROMOTION_PENDING_KEYS)
    assert pending.get("restored_by_ci_unresolved_guard") is True


# ===========================================================================
# Fix X: infra-flake detection + re-run in _harvest_promotion_ci_failures
# ===========================================================================


@pytest.mark.asyncio
async def test_harvest_infra_flake_requests_strategic_and_skips_spawn(db_session, mocker):
    """When CI failure logs contain npm-registry / Docker infra patterns,
    hand the delivery decision to Strategic through its coalesced refresh marker
    and do not directly qualify, rerun dead CI, or spawn a code fix."""
    from app.release_reconciliation import _harvest_promotion_ci_failures

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="main"
    )
    auto_remediation_config = {"enabled": True}

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "build", "conclusion": "failure"}],
                "failed_logs": "npm error code ECONNREFUSED connecting to registry.npmjs.org",
                "setup_phase_failure": False,
                "failed_run_ids": [12345],
            }
        ),
    )
    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs",
        new=AsyncMock(return_value=True),
    )
    rq_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    refresh_mock = mocker.patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=True),
    )
    handle_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure",
        new=AsyncMock(),
    )

    await _harvest_promotion_ci_failures(
        project_id=_project.id,
        version_id=version.id,
        version_number=version.version_number,
        owner="acme",
        repo="widget",
        ci_source_sha="dead" * 10,
        integration_branch="automain",
        auto_remediation_config=auto_remediation_config,
        selfheal_enabled=True,
    )

    rq_mock.assert_not_awaited()
    refresh_mock.assert_awaited_once()
    assert refresh_mock.await_args.args[1] == _project.id
    assert refresh_mock.await_args.args[2].startswith(
        f"promotion_ci_infra_flake:v{version.version_number}:"
    )
    rerun_mock.assert_not_awaited()
    handle_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_harvest_setup_phase_flake_requests_strategic_and_skips_spawn(
    db_session, mocker
):
    """A setup flake uses the same coalesced Strategic handoff."""
    from app.release_reconciliation import _harvest_promotion_ci_failures

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="main"
    )

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "build", "conclusion": "failure"}],
                "failed_logs": "Set up job failed",
                "setup_phase_failure": True,
                "failed_run_ids": [99999],
            }
        ),
    )
    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs",
        new=AsyncMock(return_value=True),
    )
    rq_mock = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    refresh_mock = mocker.patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=True),
    )
    handle_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure",
        new=AsyncMock(),
    )

    await _harvest_promotion_ci_failures(
        project_id=_project.id,
        version_id=version.id,
        version_number=version.version_number,
        owner="acme",
        repo="widget",
        ci_source_sha="cafe" * 10,
        integration_branch="automain",
        auto_remediation_config={"enabled": True},
        selfheal_enabled=True,
    )

    rq_mock.assert_not_awaited()
    refresh_mock.assert_awaited_once()
    rerun_mock.assert_not_awaited()
    handle_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_harvest_real_failure_spawns_fix(db_session, mocker):
    """When CI logs do NOT match infra patterns, the fix task is spawned normally."""
    from app.release_reconciliation import _harvest_promotion_ci_failures

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="main"
    )

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "pytest", "conclusion": "failure"}],
                "failed_logs": "FAILED testing/test_foo.py::test_bar\nAssertionError",
                "setup_phase_failure": False,
                "failed_run_ids": [],
            }
        ),
    )
    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs",
        new=AsyncMock(return_value=True),
    )
    handle_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure",
        new=AsyncMock(),
    )

    await _harvest_promotion_ci_failures(
        project_id=_project.id,
        version_id=version.id,
        version_number=version.version_number,
        owner="acme",
        repo="widget",
        ci_source_sha="beef" * 10,
        integration_branch="automain",
        auto_remediation_config={"enabled": True},
        selfheal_enabled=True,
    )

    rerun_mock.assert_not_awaited()
    handle_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_harvest_flake_budget_tristate(db_session, mocker):
    """``flake_rerun_budget`` is the bound on the live-branch rerun loop.

    ``>0`` re-runs and reports ``flake_rerun`` so the caller can consume budget;
    ``0`` (budget spent, gate still red) stops re-running and escalates the
    flake to a fix spawn instead of looping the full matrix forever.
    """
    from app.release_reconciliation import _harvest_promotion_ci_failures

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="main"
    )
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "build", "conclusion": "failure"}],
                "failed_logs": "npm error code ECONNREFUSED registry.npmjs.org",
                "setup_phase_failure": False,
                "failed_run_ids": [777],
            }
        ),
    )
    rerun_mock = mocker.patch(
        "app.github_checks.rerun_failed_jobs", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.strategic_trigger.request_strategic_refresh",
        new=AsyncMock(return_value=True),
    )
    handle_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure", new=AsyncMock()
    )

    kwargs = dict(
        project_id=_project.id,
        version_id=version.id,
        version_number=version.version_number,
        owner="acme",
        repo="widget",
        ci_source_sha="dead" * 10,
        integration_branch="automain",
        auto_remediation_config={"enabled": True},
        selfheal_enabled=True,
    )

    assert (
        await _harvest_promotion_ci_failures(**kwargs, flake_rerun_budget=1)
        == "flake_rerun"
    )
    rerun_mock.assert_awaited_once_with("tok", "acme", "widget", 777)
    handle_mock.assert_not_awaited()

    # Budget exhausted: no further rerun, escalate to the fix path.
    assert (
        await _harvest_promotion_ci_failures(**kwargs, flake_rerun_budget=0)
        == "fix_spawned"
    )
    assert rerun_mock.await_count == 1
    handle_mock.assert_awaited_once()

    # The promotion re-driver entry point (None) is unchanged: hand off, never
    # rerun the dead release branch's workflow, never spawn a fix.
    assert await _harvest_promotion_ci_failures(**kwargs) == "flake_handoff"
    assert rerun_mock.await_count == 1
    assert handle_mock.await_count == 1


# ===========================================================================
# Activation receipt: blueprint deploy → _record_activation_receipt
# ===========================================================================


@pytest.mark.asyncio
async def test_record_activation_receipt_resolves_unconfirmed_to_serving_confirmed(
    db_session,
):
    """An activation receipt for an unconfirmed promoted version confirms the
    backend artifact with the reported revision and resolves the whole version
    to serving_confirmed through the shared recovery machinery: scalar flip,
    served_sha/served_at, verdict outcome, receipt provenance."""
    from app.release_reconciliation import _record_activation_receipt

    now = _utcnow()
    promoted_at = now - timedelta(hours=1)
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )

    receipt = await _record_activation_receipt(
        db_session,
        version,
        revision=_SHA_B,
        host="hamsterwheel",
        operator_id="op@example.com",
        now=now,
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert receipt["serving_status"] == "serving_confirmed"
    assert version.serving_status == "serving_confirmed"
    assert version.served_sha == _SHA_B
    assert version.served_at is not None
    assert version.deploy_failure_reason is None

    # The receipt provenance is stamped on the version audit trail.
    receipt_block = (version.last_qualification or {}).get("activation_receipt") or {}
    assert receipt_block.get("revision") == _SHA_B
    assert receipt_block.get("host") == "hamsterwheel"
    assert receipt_block.get("operator") == "op@example.com"

    # The backend artifact is confirmed with the served identifier.
    backend = (
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
        .first()
    )
    assert backend is not None
    assert backend.serving_status == "serving_confirmed"
    assert backend.served_identifier == _SHA_B

    # The verdict outcome flipped to serving_confirmed.
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


@pytest.mark.asyncio
async def test_record_activation_receipt_leaves_aggregate_unconfirmed_when_required_pending(
    db_session, mocker
):
    """A required non-backend artifact that stays pending keeps the whole
    version unconfirmed — only the backend artifact is confirmed by the
    receipt, and the confirm-sweep finishes the remaining gate."""
    from app.release_reconciliation import _record_activation_receipt

    async def _never_resolve(_session, _version, _artifact, _now):
        return None

    backend_provider = artifact_registry._ARTIFACT_PROVIDERS[
        BACKEND_REVISION_ARTIFACT_KIND
    ]
    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {
            BACKEND_REVISION_ARTIFACT_KIND: backend_provider,
            "synthetic_pending": ReleaseArtifactProvider(
                artifact_kind="synthetic_pending",
                check=_never_resolve,
                required=True,
            ),
        },
        clear=True,
    )
    now = _utcnow()
    promoted_at = now - timedelta(hours=1)
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline
    )

    receipt = await _record_activation_receipt(
        db_session,
        version,
        revision=_SHA_B,
        host="hamsterwheel",
        operator_id="op@example.com",
        now=now,
    )
    await db_session.commit()
    await db_session.refresh(version)

    # The whole-release aggregate stays unconfirmed.
    assert receipt["serving_status"] == "unconfirmed"

    # The backend artifact is confirmed with the served identifier.
    backend = (
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
        .first()
    )
    assert backend is not None
    assert backend.serving_status == "serving_confirmed"
    assert backend.served_identifier == _SHA_B

    # The synthetic required artifact is still pending, keeping the aggregate
    # unconfirmed until the sweep resolves it.
    pending = (
        (
            await db_session.execute(
                _select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind == "synthetic_pending",
                )
            )
        )
        .scalars()
        .first()
    )
    assert pending is not None
    assert (pending.serving_status or "unconfirmed") == "unconfirmed"


# ===========================================================================
# Approved activation control: _approved_activation_mismatch /
# _approved_activation_superseded / approved-mode receipt provenance
# ===========================================================================


def test_approved_activation_mismatch_only_guards_live_approved_receipts():
    """``_approved_activation_mismatch`` rejects ONLY the case an in-product
    approval intends to close: an active approval (still unconfirmed) reporting
    a revision other than the approved promoted SHA.  No approval (the legacy
    CLI path), a matching SHA, or an already-resolved version all pass through
    unchanged."""
    from app.release_activation import _approved_activation_mismatch

    version = rv.ReleaseVersion(
        project_id=uuid4(),
        version_number=1,
        status="promoted",
        serving_status="unconfirmed",
        promoted_sha=_SHA_A,
    )

    # No approval → the legacy CLI path is unchanged even for a differing
    # revision (the built revision may legitimately be a descendant).
    assert _approved_activation_mismatch(version, _SHA_B) is None

    # Active approval + the EXACT approved SHA → no rejection.
    version.activation_requested_at = _utcnow()
    version.activation_requested_by = "op@example.com"
    assert _approved_activation_mismatch(version, _SHA_A) is None

    # Active approval + a DIFFERENT revision → mismatch rejection naming the
    # approved SHA prefix so the operator can see which artifact is authorized.
    detail = _approved_activation_mismatch(version, "9" * 40)
    assert detail is not None
    assert "does not match" in detail
    assert _SHA_A[:12] in detail

    # Already-resolved → the existing idempotency / recovery machinery governs.
    version.serving_status = "serving_confirmed"
    assert _approved_activation_mismatch(version, _SHA_B) is None


@pytest.mark.asyncio
async def test_approved_activation_superseded_detects_newer_promotion(db_session):
    """An approved version is superseded once a NEWER promotion exists; the
    current promoted version is never superseded by itself."""
    from app.release_activation import _approved_activation_superseded

    now = _utcnow()
    promoted_at = now - timedelta(hours=2)
    deadline = promoted_at + timedelta(hours=1)
    project, v1 = await _make_promoted_unconfirmed_version(
        db_session, promoted_at=promoted_at, deadline=deadline, promoted_sha=_SHA_A
    )
    v1.activation_requested_at = now
    v1.activation_requested_by = "op@example.com"
    db_session.add(v1)
    await db_session.commit()

    assert await _approved_activation_superseded(db_session, v1) is False

    # A newer promotion supersedes the approved v1.
    v2 = await rv.open_version(db_session, project.id, version_number=2)
    v2.status = "promoted"
    v2.promoted_at = now
    v2.promoted_sha = _SHA_B
    v2.serving_status = "unconfirmed"
    v2.deploy_confirm_deadline = now + timedelta(hours=1)
    db_session.add(v2)
    await db_session.commit()

    assert await _approved_activation_superseded(db_session, v1) is True
    assert await _approved_activation_superseded(db_session, v2) is False


@pytest.mark.asyncio
async def test_record_activation_receipt_approved_mode_stamps_provenance(db_session):
    """A receipt under an in-product approval records mode=approved_activation
    and the approval binding it fulfilled, alongside the standard provenance."""
    from app.release_reconciliation import _record_activation_receipt

    now = _utcnow()
    promoted_at = now - timedelta(hours=1)
    deadline = promoted_at + timedelta(hours=1)
    _project, version = await _make_promoted_unconfirmed_version(
        db_session,
        promoted_at=promoted_at,
        deadline=deadline,
        promoted_sha=_SHA_A,
    )
    version.activation_requested_at = promoted_at + timedelta(minutes=30)
    version.activation_requested_by = "op@example.com"
    db_session.add(version)
    await db_session.commit()

    receipt = await _record_activation_receipt(
        db_session,
        version,
        revision=_SHA_A,
        host="hamsterwheel",
        operator_id="op@example.com",
        now=now,
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert receipt["mode"] == "approved_activation"
    assert version.serving_status == "serving_confirmed"

    receipt_block = (version.last_qualification or {}).get("activation_receipt") or {}
    assert receipt_block["mode"] == "approved_activation"
    assert receipt_block["revision"] == _SHA_A
    assert receipt_block["activation_requested_by"] == "op@example.com"
    assert receipt_block["activation_requested_at"] is not None


# ===========================================================================
# Promotion-CI self-heal gate: decoupled from design_doc._auto_remediation,
# governed by durable operator-owned build_config (default ON, project-agnostic)
# ===========================================================================


def test_promotion_ci_selfheal_enabled_resolver():
    """The release-pipeline self-heal gate resolves from durable, operator-owned
    build_config (NOT the strategist-clobbered design_doc._auto_remediation) and
    defaults ON for any project — no self-repo / repo-name special-casing."""
    from app.release_reconciliation import promotion_ci_selfheal_enabled

    # No build_config at all -> default ON.
    p = Project(name="p", user_id="u", github_owner="o", github_repo="r")
    assert promotion_ci_selfheal_enabled(p) is True

    # Explicit operator opt-out in build_config wins.
    p.build_config = {"promotion_ci_selfheal_enabled": False}
    assert promotion_ci_selfheal_enabled(p) is False

    # Explicit operator opt-in.
    p.build_config = {"promotion_ci_selfheal_enabled": True}
    assert promotion_ci_selfheal_enabled(p) is True

    # A non-bool value is ignored -> default ON (robust to malformed config).
    p.build_config = {"promotion_ci_selfheal_enabled": "yes"}
    assert promotion_ci_selfheal_enabled(p) is True

    # design_doc._auto_remediation.enabled does NOT gate this anymore: even a
    # FALSE/absent _auto_remediation leaves the release self-heal ON by default.
    p.build_config = None
    p.design_doc = {"_auto_remediation": {"enabled": False}}
    assert promotion_ci_selfheal_enabled(p) is True


@pytest.mark.asyncio
async def test_harvest_skips_when_selfheal_disabled(db_session, mocker):
    """selfheal_enabled=False makes the harvest a no-op: no infra-flake re-qualify
    and no fix-task spawn, and it returns BEFORE any GitHub I/O — regardless of
    auto_remediation_config."""
    from app.release_reconciliation import _harvest_promotion_ci_failures

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="main"
    )
    rq_mock = mocker.patch(
        "app.release_qualifier.run_qualification", new=AsyncMock()
    )
    handle_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure", new=AsyncMock()
    )
    token_mock = mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )

    await _harvest_promotion_ci_failures(
        project_id=_project.id,
        version_id=version.id,
        version_number=version.version_number,
        owner="acme",
        repo="widget",
        ci_source_sha="dead" * 10,
        integration_branch="automain",
        # Even an *enabled* generic auto_remediation config must not re-enable a
        # self-heal the operator turned OFF at the release level.
        auto_remediation_config={"enabled": True},
        selfheal_enabled=False,
    )
    rq_mock.assert_not_awaited()
    handle_mock.assert_not_awaited()
    token_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_harvest_self_heals_with_empty_auto_remediation_config(
    db_session, mocker
):
    """The promotion-CI self-heal is decoupled from design_doc._auto_remediation:
    with selfheal_enabled=True it spawns the fix even when auto_remediation_config
    is EMPTY (the absent-config case that left Blueprint's own promotions
    un-harvested before this change). The empty config still drives the spawn
    mechanics via built-in defaults."""
    from app.release_reconciliation import _harvest_promotion_ci_failures

    _project, version = await _make_project_and_version(
        db_session, integration_branch="automain", promotion_target="main"
    )
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="tok"),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={
                "status": "completed",
                "conclusion": "failure",
                "details": [{"name": "pytest", "conclusion": "failure"}],
                "failed_logs": "FAILED testing/test_foo.py::test_bar\nAssertionError",
                "setup_phase_failure": False,
                "failed_run_ids": [],
            }
        ),
    )
    handle_mock = mocker.patch(
        "app.auto_remediation._handle_main_branch_ci_failure", new=AsyncMock()
    )

    await _harvest_promotion_ci_failures(
        project_id=_project.id,
        version_id=version.id,
        version_number=version.version_number,
        owner="acme",
        repo="widget",
        ci_source_sha="beef" * 10,
        integration_branch="automain",
        # Absent / never-customized generic config (Blueprint's own case).
        auto_remediation_config={},
        selfheal_enabled=True,
    )
    handle_mock.assert_awaited_once()


# ===========================================================================
# Fix X: _is_ci_infra_flake_log pattern matching
# ===========================================================================


def test_is_ci_infra_flake_log_detects_npm_error():
    """npm registry connection errors are detected as infra flakes."""
    from app.failure_classification import _is_ci_infra_flake_log

    assert _is_ci_infra_flake_log("npm error code ECONNREFUSED\nsome other output")
    assert _is_ci_infra_flake_log("npm error code ETIMEDOUT connecting to registry.npmjs.org")
    assert _is_ci_infra_flake_log("npm ERR! network Connection refused")


def test_is_ci_infra_flake_log_detects_playwright():
    """Playwright install failures are detected as infra flakes."""
    from app.failure_classification import _is_ci_infra_flake_log

    assert _is_ci_infra_flake_log("playwright install failed: network error")
