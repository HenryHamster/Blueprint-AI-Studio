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

from app import release_version as rv
from app.models import Project


async def _promote(db_session):
    """Create a committed project + open version and record its promotion.

    Returns ``(result, project, version)``. The project is committed so the
    driver's OWN ``AsyncSessionLocal`` session (opened inside the best-effort
    refresh block) can re-fetch it.
    """
    from app.release_promotion import _record_promotion_success

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
