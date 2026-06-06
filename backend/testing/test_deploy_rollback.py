"""Endpoint tests for the deploy-state rollback control.

These cover the idempotency / pause-clock correctness fixes for
``trigger_rollback`` in ``app.routers.deploy``:

  * a rollback from STABLE snapshots the genuine live VM capacity, zeroes
    live capacity, and marks the pause window on the pause clock;
  * a rollback while the pipeline is ALREADY paused (auto or manual) must
    NOT clobber the genuine pre-pause capacity snapshot, and must preserve
    the existing pause window (so accumulated pause-time isn't lost);
  * resume after a rollback restores the operator's real capacity, not the
    default fallback;
  * the consecutive-rollback counter increments exactly once per rollback.

The new test file (rather than appending to test_admin_routers.py) avoids
colliding with a sibling task that is editing that file.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app import pause_clock
from app.models import DeploymentState, DeploymentStatus
from app.shared_state import runtime_config


def _rollbackable_state(**overrides) -> DeploymentState:
    """A DeploymentState that has a distinct last-known-good revision so the
    rollback precondition passes."""
    defaults = dict(
        current_revision="rev-bad",
        current_sha="sha123bad",
        last_known_good_revision="rev-good",
        last_known_good_sha="sha123good",
        status=DeploymentStatus.STABLE,
        rollback_count=0,
        total_manual_paused_seconds=0.0,
        total_auto_paused_seconds=0.0,
        paused_at=None,
        paused_kind=None,
    )
    defaults.update(overrides)
    return DeploymentState(**defaults)


def _gcloud_ok():
    """Patch subprocess.run to a successful gcloud traffic shift."""
    p = patch("subprocess.run")
    mock_run = p.start()
    mock_run.return_value.returncode = 0
    mock_run.return_value.stderr = ""
    return p


@pytest.mark.asyncio
async def test_rollback_from_stable_snapshots_real_capacity(
    client: AsyncClient, db_session
):
    """Rolling back from STABLE captures the genuine live capacity, zeroes live
    capacity, and records the pause window on the pause clock."""
    db_session.add(_rollbackable_state())
    await db_session.commit()

    runtime_config["max_concurrent_vms"] = 5

    p = _gcloud_ok()
    try:
        resp = await client.post("/api/admin/deploy-state/rollback")
    finally:
        p.stop()

    assert resp.status_code == 200
    assert resp.json()["pipeline_paused"] is True
    assert runtime_config["max_concurrent_vms"] == 0

    state = await db_session.get(DeploymentState, 1)
    await db_session.refresh(state)
    assert state.status == DeploymentStatus.ROLLED_BACK
    assert state.pre_pause_max_vms == 5
    assert state.paused_at is not None
    assert state.paused_kind == pause_clock.PAUSED_KIND_AUTO
    assert state.rollback_count == 1


@pytest.mark.asyncio
async def test_rollback_while_paused_preserves_real_capacity(
    client: AsyncClient, db_session
):
    """A rollback issued while the pipeline is already (auto-)paused must not
    overwrite the genuine pre_pause_max_vms snapshot with the already-zeroed
    live capacity, and must preserve the existing pause window."""
    paused_at = datetime.utcnow() - timedelta(seconds=300)
    db_session.add(
        _rollbackable_state(
            status=DeploymentStatus.PAUSED,
            pre_pause_max_vms=5,
            runtime_max_concurrent_vms=5,
            paused_at=paused_at,
            paused_kind=pause_clock.PAUSED_KIND_AUTO,
        )
    )
    await db_session.commit()

    # Pipeline already paused — live capacity is zero.
    runtime_config["max_concurrent_vms"] = 0

    p = _gcloud_ok()
    try:
        resp = await client.post("/api/admin/deploy-state/rollback")
    finally:
        p.stop()

    assert resp.status_code == 200
    state = await db_session.get(DeploymentState, 1)
    await db_session.refresh(state)
    # Genuine capacity preserved, NOT clobbered to 0.
    assert state.pre_pause_max_vms == 5
    # Existing pause window preserved (mark_paused is idempotent).
    assert state.paused_at == paused_at
    assert state.paused_kind == pause_clock.PAUSED_KIND_AUTO


@pytest.mark.asyncio
async def test_resume_after_rollback_restores_real_capacity(
    client: AsyncClient, db_session
):
    """After a rollback from STABLE, resume restores the operator's real
    capacity (5), not the default fallback (5 is coincidental — prove it by
    using a non-default value)."""
    db_session.add(_rollbackable_state())
    await db_session.commit()

    runtime_config["max_concurrent_vms"] = 9

    p = _gcloud_ok()
    try:
        resp = await client.post("/api/admin/deploy-state/rollback")
        assert resp.status_code == 200
        assert runtime_config["max_concurrent_vms"] == 0

        resume = await client.post("/api/admin/deploy-state/resume")
    finally:
        p.stop()

    assert resume.status_code == 200
    assert resume.json()["max_concurrent_vms"] == 9
    assert runtime_config["max_concurrent_vms"] == 9


@pytest.mark.asyncio
async def test_rollback_double_submit_is_rejected_after_first(
    client: AsyncClient, db_session
):
    """A second rollback after the first transitions to ROLLED_BACK is rejected,
    so the consecutive-rollback counter increments exactly once."""
    db_session.add(_rollbackable_state())
    await db_session.commit()

    runtime_config["max_concurrent_vms"] = 5

    p = _gcloud_ok()
    try:
        first = await client.post("/api/admin/deploy-state/rollback")
        assert first.status_code == 200

        second = await client.post("/api/admin/deploy-state/rollback")
    finally:
        p.stop()

    # Already rolled back — second is rejected, no double-count.
    assert second.status_code == 400
    state = await db_session.get(DeploymentState, 1)
    await db_session.refresh(state)
    assert state.rollback_count == 1


# ---------------------------------------------------------------------------
# Row-lock serialization of the manual levers (this task).
#
# The five operator levers now read AND mutate the DeploymentState singleton
# under the same SELECT-FOR-UPDATE discipline the automated monitor uses
# (deployment_state_lock). These tests assert the two observable contracts of
# that change: (1) lock contention surfaces a retry-friendly 429 (not a 5xx,
# not a silent clobber) while leaving the row untouched, and (2) a successful
# lever bumps the optimistic-concurrency ``version`` so concurrent writers can
# detect a lost-update race.
#
# True DB-level interleaving (one connection holding the lock while another
# skips) can't be reproduced on the SQLite StaticPool harness — every session
# shares one connection there — so the held-row case is exercised by patching
# ``deployment_state_lock`` to yield None against a row that genuinely exists,
# which is exactly the state a SKIP-LOCKED acquisition reports under contention.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _held_lock(session, **kwargs):
    """Stand-in for deployment_state_lock when the row is held by a peer:
    SKIP LOCKED yields None even though the row exists."""
    yield None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/deploy-state/pause",
        "/api/admin/deploy-state/resume",
        "/api/admin/deploy-state/reset-health",
        "/api/admin/deploy-state/mark-stable",
        "/api/admin/deploy-state/rollback",
    ],
)
async def test_manual_lever_returns_429_when_row_held(
    client: AsyncClient, db_session, path
):
    """When the singleton is held by a concurrent writer, every manual lever
    returns 429 (retry) with a Retry-After header and does NOT mutate the row."""
    db_session.add(
        _rollbackable_state(
            status=DeploymentStatus.PAUSED,
            pre_pause_max_vms=7,
            runtime_max_concurrent_vms=7,
            paused_at=datetime.utcnow() - timedelta(seconds=120),
            paused_kind=pause_clock.PAUSED_KIND_AUTO,
        )
    )
    await db_session.commit()

    runtime_config["max_concurrent_vms"] = 0

    p = _gcloud_ok()
    try:
        with patch("app.routers.deploy.deployment_state_lock", _held_lock):
            resp = await client.post(path)
    finally:
        p.stop()

    # 429 (retry), not a 5xx — Cloud Run quarantines instances on 5xx. (The
    # app's global HTTPException handler renders the status + detail but does not
    # forward the exception's Retry-After header, so the contract asserted here
    # is the retry-friendly status, matching lock_project_or_429's behavior.)
    assert resp.status_code == 429

    # Row untouched — the held lock prevented any mutation.
    state = await db_session.get(DeploymentState, 1)
    await db_session.refresh(state)
    assert state.status == DeploymentStatus.PAUSED
    assert state.pre_pause_max_vms == 7
    assert state.rollback_count == 0
    assert state.version == 0


@pytest.mark.asyncio
async def test_pause_bumps_version_for_lost_update_detection(
    client: AsyncClient, db_session
):
    """A successful manual pause bumps the optimistic-concurrency version so a
    concurrent unlocked writer could detect the lost-update race."""
    db_session.add(_rollbackable_state(status=DeploymentStatus.STABLE))
    await db_session.commit()
    state = await db_session.get(DeploymentState, 1)
    assert state.version == 0

    runtime_config["max_concurrent_vms"] = 5
    resp = await client.post("/api/admin/deploy-state/pause")
    assert resp.status_code == 200

    await db_session.refresh(state)
    assert state.status == DeploymentStatus.PAUSED
    assert state.version == 1
    assert state.pre_pause_max_vms == 5


@pytest.mark.asyncio
async def test_resume_bumps_version_and_single_commit(
    client: AsyncClient, db_session
):
    """Resume runs as one locked read-check-mutate-commit (the legacy double
    commit is gone) and bumps the version exactly once."""
    db_session.add(
        _rollbackable_state(
            status=DeploymentStatus.PAUSED,
            pre_pause_max_vms=8,
            runtime_max_concurrent_vms=8,
            paused_at=datetime.utcnow() - timedelta(seconds=60),
            paused_kind=pause_clock.PAUSED_KIND_MANUAL,
        )
    )
    await db_session.commit()
    state = await db_session.get(DeploymentState, 1)
    assert state.version == 0

    runtime_config["max_concurrent_vms"] = 0
    resp = await client.post("/api/admin/deploy-state/resume")
    assert resp.status_code == 200
    assert resp.json()["max_concurrent_vms"] == 8

    await db_session.refresh(state)
    assert state.status == DeploymentStatus.STABLE
    assert state.pre_pause_max_vms is None
    # One commit inside one lock window → exactly one version bump.
    assert state.version == 1
    assert runtime_config["max_concurrent_vms"] == 8


@pytest.mark.asyncio
async def test_resume_rechecks_precondition_against_committed_state(
    client: AsyncClient, db_session
):
    """If an automated transition already moved the row out of PAUSED and
    committed, a manual resume re-checks the precondition against that
    freshly-loaded locked row and is rejected (no clobber of the auto state)."""
    db_session.add(_rollbackable_state(status=DeploymentStatus.STABLE))
    await db_session.commit()

    runtime_config["max_concurrent_vms"] = 5
    resp = await client.post("/api/admin/deploy-state/resume")

    # STABLE is not a resumable state — precondition evaluated on the locked row.
    assert resp.status_code == 400
    state = await db_session.get(DeploymentState, 1)
    await db_session.refresh(state)
    assert state.status == DeploymentStatus.STABLE
    # Rejected before any mutation → no version bump.
    assert state.version == 0
