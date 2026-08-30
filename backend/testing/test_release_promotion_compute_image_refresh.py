"""Event-driven compute-image refresh on release promotion.

Covers the promotion-success half of the "apply at the event AND self-heal via
a periodic sweep" pattern: when ``_record_promotion_success`` records a
successful promotion it fires a best-effort, fire-and-forget compute-image
refresh for the promoted project so a dependency-manifest change that landed in
the promoted commit re-pins the baked image immediately instead of waiting for
the hourly sweep.

Hard contract asserted here:
- a successful promotion INVOKES the single-project refresh driver for the
  promoted project, handed the SAME process-wide build concurrency semaphore the
  periodic sweep and admin endpoint use;
- a driver that RAISES never breaks the promotion (return dict still
  ``status="promoted"`` with the next version opened);
- a driver that returns a ``builder_busy``/``deferred`` no-op leaves the
  promotion fully intact.
"""

import pytest
from sqlalchemy import func, select

from app import release_version as rv
from app.models import Project, ProjectMessage, QualificationVerdict, ReleaseVersion


async def _seed_promotion(db_session):
    project = Project(
        name="Refresh-Repo",
        user_id="rr-user",
        github_owner="acme",
        github_repo="refresh",
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
    return project, version


async def _promote(db_session):
    """Create a committed project + open version and record its promotion.

    Returns ``(result, project, version)``. The project is committed so the
    driver's OWN ``AsyncSessionLocal`` session (opened inside the best-effort
    refresh block) can re-fetch it.
    """
    from app.release_promotion import _record_promotion_success

    project, version = await _seed_promotion(db_session)

    result = await _record_promotion_success(
        db_session,
        version,
        candidate_sha="c" * 40,
        base_record={"confidence": 0.9, "status": "promoted"},
        pr_url="http://pr/1",
        promoted_sha="p" * 40,
        lock_released=True,
    )
    await db_session.commit()
    return result, project, version


def _patch_postcommit_effects(mocker, *, activation=None):
    effects = {
        "activation": mocker.patch(
            "app.directions_repo.activate_eligible_staged_directions",
            new=mocker.AsyncMock(side_effect=activation, return_value=[]),
        ),
        "refresh": mocker.patch(
            "app.strategic_trigger.request_strategic_refresh",
            new=mocker.AsyncMock(return_value=True),
        ),
        "tag": mocker.patch(
            "app.github.create_tag",
            new=mocker.AsyncMock(return_value=True),
        ),
        "baseline": mocker.patch(
            "app.lane_baseline_snapshot.persist_version_lane_baseline",
            new=mocker.AsyncMock(return_value={"stored": True}),
        ),
        "supersession": mocker.patch(
            "app.message_supersession.run_supersession_on_version_close",
            new=mocker.AsyncMock(return_value={"acted": False}),
        ),
        "compute": mocker.patch(
            "app.compute_backends.refresh_project_compute_image",
            new=mocker.AsyncMock(return_value={"status": "unchanged"}),
        ),
    }
    return effects


async def _record_success(db_session, version):
    from app.release_promotion import _record_promotion_success

    return await _record_promotion_success(
        db_session,
        version,
        candidate_sha="c" * 40,
        base_record={"confidence": 0.9, "status": "promoted"},
        pr_url="http://pr/1",
        promoted_sha="p" * 40,
        token="token",
        owner="acme",
        repo="refresh",
        lock_released=True,
    )


@pytest.mark.asyncio
async def test_promotion_invokes_compute_image_refresh_with_shared_semaphore(
    db_session, mocker
):
    """A successful promotion drives the single-project compute-image refresh for
    the promoted project, passing the shared build-concurrency semaphore."""
    from app.routers.admin_project_tools import _compute_image_build_semaphore

    refresh = mocker.patch(
        "app.compute_backends.refresh_project_compute_image",
        new=mocker.AsyncMock(return_value={"status": "unchanged"}),
    )

    result, project, _version = await _promote(db_session)

    assert result["status"] == "promoted"
    # Driver was invoked exactly once for the promoted project.
    refresh.assert_awaited_once()
    call = refresh.await_args
    # project is the first positional arg after the driver's own session.
    passed_project = call.args[1]
    assert passed_project.id == project.id
    # The SAME process-wide guard the sweep/admin endpoint use — not a fresh one.
    assert call.kwargs["build_semaphore"] is _compute_image_build_semaphore


@pytest.mark.asyncio
async def test_promotion_survives_refresh_driver_raising(db_session, mocker):
    """A refresh driver that RAISES is swallowed — the promotion still records
    success and opens the next version."""
    refresh = mocker.patch(
        "app.compute_backends.refresh_project_compute_image",
        new=mocker.AsyncMock(side_effect=RuntimeError("docker exploded")),
    )

    result, _project, version = await _promote(db_session)

    refresh.assert_awaited_once()
    assert result["status"] == "promoted"
    assert result["promoted_sha"] == "p" * 40
    assert result["next_version_number"] == 2
    await db_session.refresh(version)
    assert version.status == "promoted"


@pytest.mark.asyncio
async def test_promotion_survives_refresh_builder_busy_noop(db_session, mocker):
    """A ``deferred``/builder-busy no-op result (a build already in flight) is a
    clean no-op that leaves the promotion fully intact."""
    refresh = mocker.patch(
        "app.compute_backends.refresh_project_compute_image",
        new=mocker.AsyncMock(
            return_value={"status": "deferred", "reason": "builder_busy"}
        ),
    )

    result, _project, _version = await _promote(db_session)

    refresh.assert_awaited_once()
    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2


@pytest.mark.asyncio
async def test_promotion_precommit_failure_runs_no_postcommit_effects(db_session, mocker):
    from conftest import TestingSessionLocal

    project, version = await _seed_promotion(db_session)
    project_id = project.id
    version_id = version.id
    effects = _patch_postcommit_effects(mocker)
    mocker.patch.object(
        db_session,
        "commit",
        new=mocker.AsyncMock(side_effect=RuntimeError("canonical commit failed")),
    )

    with pytest.raises(RuntimeError, match="canonical commit failed"):
        await _record_success(db_session, version)
    await db_session.rollback()
    for effect in effects.values():
        effect.assert_not_awaited()

    async with TestingSessionLocal() as verify_session:
        rows = (
            await verify_session.execute(
                select(ReleaseVersion).where(ReleaseVersion.project_id == project_id)
            )
        ).scalars().all()
        assert [(row.version_number, row.status) for row in rows] == [(1, "open")]
        assert await verify_session.scalar(
            select(func.count()).select_from(QualificationVerdict).where(
                QualificationVerdict.release_version_id == version_id
            )
        ) == 0
        assert await verify_session.scalar(
            select(func.count()).select_from(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key
                == f"release:{version_id}:release_promoted",
            )
        ) == 0


@pytest.mark.asyncio
async def test_promotion_commit_ack_loss_retries_without_second_successor(
    db_session, mocker
):
    from conftest import TestingSessionLocal

    project, version = await _seed_promotion(db_session)
    project_id = project.id
    effects = _patch_postcommit_effects(mocker)
    real_commit = db_session.commit
    commit_count = 0

    async def commit_then_lose_ack():
        nonlocal commit_count
        commit_count += 1
        await real_commit()
        if commit_count == 1:
            raise ConnectionError("commit acknowledgement lost")

    mocker.patch.object(
        db_session,
        "commit",
        new=mocker.AsyncMock(side_effect=commit_then_lose_ack),
    )
    with pytest.raises(ConnectionError, match="acknowledgement lost"):
        await _record_success(db_session, version)
    for effect in effects.values():
        effect.assert_not_awaited()

    async with TestingSessionLocal() as verify_session:
        rows = (
            await verify_session.execute(
                select(ReleaseVersion).where(ReleaseVersion.project_id == project_id)
            )
        ).scalars().all()
        assert sorted((row.version_number, row.status) for row in rows) == [
            (1, "promoted"),
            (2, "open"),
        ]

    retry = await _record_success(db_session, version)
    assert retry["already_promoted"] is True
    assert retry["next_version_number"] is None
    await db_session.rollback()
    async with TestingSessionLocal() as verify_session:
        assert await verify_session.scalar(
            select(func.count()).select_from(ReleaseVersion).where(
                ReleaseVersion.project_id == project_id,
                ReleaseVersion.version_number == 2,
            )
        ) == 1


@pytest.mark.asyncio
async def test_first_postcommit_hook_observes_complete_canonical_transaction(
    db_session, mocker
):
    project, version = await _seed_promotion(db_session)
    observed = False

    async def observe_canonical_state(session, project_id, *_args, **_kwargs):
        nonlocal observed
        rows = (
            await session.execute(
                select(ReleaseVersion).where(ReleaseVersion.project_id == project_id)
            )
        ).scalars().all()
        assert sorted((row.version_number, row.status, row.base_sha) for row in rows) == [
            (1, "promoted", None),
            (2, "open", "p" * 40),
        ]
        assert await session.scalar(
            select(func.count()).select_from(QualificationVerdict).where(
                QualificationVerdict.release_version_id == version.id,
                QualificationVerdict.terminal_status == "promoted",
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key
                == f"release:{version.id}:release_promoted",
            )
        ) == 1
        observed = True
        return []

    _patch_postcommit_effects(mocker, activation=observe_canonical_state)
    result = await _record_success(db_session, version)
    assert result["status"] == "promoted"
    assert observed is True
