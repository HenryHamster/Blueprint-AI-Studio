"""Tests for system_state.compute_system_state().

Covers the declared acceptance evidence:
1. Serving-version resolution is aggregate-first (aggregate beats scalar).
2. deploy_failed detection surfaces through ServingSnapshot.
3. PAUSED/HALTED DeploymentState is reflected in DeployStateSnapshot.
4. FleetSnapshot counts map task statuses correctly.
5. Cross-surface agreement: compute_system_state().version.serving.status
   equals the aggregate_serving_status that summarize_release_artifacts
   would produce for the same version.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app import release_artifacts as ra
from app import release_version as rv
from app.models import (
    DeploymentState,
    DeploymentStatus,
    Project,
    ReleaseDeployArtifact,
    Task,
    TaskStatus,
)
from app.system_state import (
    compute_system_state,
)
from app.time_utils import _utcnow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_deploy_state(
    session,
    *,
    status: str = DeploymentStatus.STABLE,
    halted: bool = False,
    pause_reason: str | None = None,
):
    ds = DeploymentState(
        id=1,
        status=status,
        pause_reason=pause_reason
        or ("paused_test" if status == DeploymentStatus.PAUSED else None),
        halted_at=_utcnow() if halted else None,
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


async def _make_project(session):
    project = Project(
        name="State-Test",
        user_id="test-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def _make_version(
    session,
    project_id,
    *,
    serving_status: str | None = None,
    served_sha: str | None = None,
    last_qualification: dict | None = None,
):
    version = await rv.open_version(session, project_id, version_number=1)
    if serving_status is not None:
        version.serving_status = serving_status
    if served_sha is not None:
        version.served_sha = served_sha
    if last_qualification is not None:
        version.last_qualification = last_qualification
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


async def _make_artifact(
    session,
    version_id,
    *,
    artifact_kind: str = ra.BACKEND_REVISION_ARTIFACT_KIND,
    serving_status: str,
    reason_code: str | None = None,
    served_identifier: str | None = None,
    served_at=None,
):
    artifact = ReleaseDeployArtifact(
        version_id=version_id,
        artifact_kind=artifact_kind,
        serving_status=serving_status,
        reason_code=reason_code,
        served_identifier=served_identifier,
        served_at=served_at,
    )
    session.add(artifact)
    await session.commit()
    return artifact


async def _make_task(session, project_id, *, status: TaskStatus, last_heartbeat=None):
    task = Task(
        project_id=project_id,
        status=status,
        last_heartbeat=last_heartbeat,
    )
    session.add(task)
    await session.commit()
    return task


# ---------------------------------------------------------------------------
# DeployStateSnapshot — status/halted/paused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_state_stable(db_session):
    """DeployStateSnapshot reflects STABLE status and halted=False."""
    await _make_deploy_state(db_session, status=DeploymentStatus.STABLE)
    snap = await compute_system_state(db_session)
    assert snap.deploy.status == DeploymentStatus.STABLE
    assert snap.deploy.halted is False
    assert snap.deploy.pause_reason is None


@pytest.mark.asyncio
async def test_deploy_state_paused(db_session):
    """DeployStateSnapshot reflects PAUSED status and pause_reason."""
    await _make_deploy_state(
        db_session, status=DeploymentStatus.PAUSED, pause_reason="dep_down"
    )
    snap = await compute_system_state(db_session)
    assert snap.deploy.status == DeploymentStatus.PAUSED
    assert snap.deploy.pause_reason == "dep_down"
    assert snap.deploy.halted is False


@pytest.mark.asyncio
async def test_deploy_state_halted(db_session):
    """DeployStateSnapshot.halted is True when halted_at is set."""
    await _make_deploy_state(db_session, status=DeploymentStatus.HALTED, halted=True)
    snap = await compute_system_state(db_session)
    assert snap.deploy.status == DeploymentStatus.HALTED
    assert snap.deploy.halted is True


@pytest.mark.asyncio
async def test_deploy_state_missing_singleton(db_session):
    """compute_system_state succeeds with status=unknown when no DeploymentState row."""
    snap = await compute_system_state(db_session)
    assert snap.deploy.status == "unknown"
    assert snap.deploy.halted is False
    assert snap.version is None


# ---------------------------------------------------------------------------
# Version block — no project_id / no open version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_project_id_returns_version_none(db_session):
    """compute_system_state with no project_id returns version=None."""
    await _make_deploy_state(db_session)
    snap = await compute_system_state(db_session)
    assert snap.version is None
    assert snap.fleet is not None


@pytest.mark.asyncio
async def test_no_open_version_returns_version_none(db_session):
    """When no open version exists for the project, version=None but snap succeeds."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    snap = await compute_system_state(db_session, project.id)
    assert snap.version is None
    assert snap.deploy.status == DeploymentStatus.STABLE


# ---------------------------------------------------------------------------
# Serving-version resolution: aggregate-first (acceptance criterion 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serving_status_aggregate_beats_scalar(db_session):
    """ServingSnapshot.status is aggregate-first: deploy_failed artifact beats scalar."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    # Version scalar says serving_confirmed, but artifact says deploy_failed.
    version = await _make_version(
        db_session, project.id, serving_status="serving_confirmed", served_sha="abc"
    )
    await _make_artifact(
        db_session,
        version.id,
        serving_status="deploy_failed",
        reason_code="rolled_back",
    )
    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    # Aggregate must dominate: deploy_failed > serving_confirmed
    assert snap.version.serving.status == "deploy_failed"
    assert snap.version.serving.failure_reason == "rolled_back"


@pytest.mark.asyncio
async def test_serving_unconfirmed_artifact_beats_confirmed_scalar(db_session):
    """Unconfirmed artifact beats a serving_confirmed scalar in aggregate-first logic."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    version = await _make_version(
        db_session, project.id, serving_status="serving_confirmed"
    )
    await _make_artifact(
        db_session,
        version.id,
        serving_status="unconfirmed",
    )
    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    assert snap.version.serving.status == "unconfirmed"
    assert snap.version.serving.failure_reason is None


# ---------------------------------------------------------------------------
# deploy_failed detection (acceptance criterion 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_failed_detection_via_artifact(db_session):
    """deploy_failed artifact surfaces through ServingSnapshot.failure_reason."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    version = await _make_version(db_session, project.id)
    await _make_artifact(
        db_session,
        version.id,
        serving_status="deploy_failed",
        reason_code="sha_mismatch",
    )
    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    assert snap.version.serving.status == "deploy_failed"
    assert snap.version.serving.failure_reason == "sha_mismatch"


@pytest.mark.asyncio
async def test_deploy_failed_failure_reason_none_for_non_failure(db_session):
    """failure_reason is None when serving_status is not deploy_failed."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    version = await _make_version(db_session, project.id)
    await _make_artifact(
        db_session,
        version.id,
        serving_status="serving_confirmed",
        served_identifier="rev-1",
        served_at=_utcnow(),
    )
    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    assert snap.version.serving.status == "serving_confirmed"
    assert snap.version.serving.failure_reason is None


# ---------------------------------------------------------------------------
# FleetSnapshot counts (acceptance criterion 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fleet_counts_active_queued_evaluating_parked(db_session):
    """FleetSnapshot correctly maps task statuses to fleet slots."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)

    await _make_task(db_session, project.id, status=TaskStatus.IN_PROGRESS)
    await _make_task(db_session, project.id, status=TaskStatus.IN_PROGRESS)
    await _make_task(db_session, project.id, status=TaskStatus.QUEUED)
    await _make_task(db_session, project.id, status=TaskStatus.PROVISIONING)
    await _make_task(db_session, project.id, status=TaskStatus.EVALUATING)
    await _make_task(db_session, project.id, status=TaskStatus.PARKED)
    # Terminal statuses must not be counted
    await _make_task(db_session, project.id, status=TaskStatus.COMPLETED)
    await _make_task(db_session, project.id, status=TaskStatus.FAILED)

    snap = await compute_system_state(db_session, project.id)
    assert snap.fleet.active == 2
    assert snap.fleet.queued == 2  # QUEUED + PROVISIONING
    assert snap.fleet.evaluating == 1
    assert snap.fleet.parked == 1


@pytest.mark.asyncio
async def test_fleet_stale_heartbeat_count(db_session):
    """Stale heartbeat count identifies IN_PROGRESS tasks with last_heartbeat > 5 min ago."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)

    stale_time = _utcnow() - timedelta(minutes=10)
    fresh_time = _utcnow() - timedelta(minutes=1)

    # One stale, one fresh, one with no heartbeat (not counted as stale)
    await _make_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=stale_time,
    )
    await _make_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=fresh_time,
    )
    await _make_task(
        db_session,
        project.id,
        status=TaskStatus.IN_PROGRESS,
        last_heartbeat=None,
    )

    snap = await compute_system_state(db_session, project.id)
    assert snap.fleet.stale_heartbeat_count == 1


@pytest.mark.asyncio
async def test_fleet_scoped_to_project(db_session):
    """Fleet counts are scoped to project_id when provided; fleet-wide when None."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    other_project = await _make_project(db_session)

    await _make_task(db_session, project.id, status=TaskStatus.IN_PROGRESS)
    await _make_task(db_session, other_project.id, status=TaskStatus.IN_PROGRESS)

    snap_project = await compute_system_state(db_session, project.id)
    assert snap_project.fleet.active == 1

    snap_global = await compute_system_state(db_session)
    assert snap_global.fleet.active == 2


# ---------------------------------------------------------------------------
# Cross-surface agreement (acceptance criterion 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_surface_serving_status_agreement(db_session):
    """compute_system_state().version.serving.status equals aggregate from summarize_release_artifacts."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    version = await _make_version(db_session, project.id)

    await _make_artifact(
        db_session,
        version.id,
        serving_status="serving_confirmed",
        served_identifier="rev-abc",
        served_at=_utcnow(),
    )

    # Compute the aggregate directly from summarize_release_artifacts
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models import ReleaseVersion

    stmt = (
        select(ReleaseVersion)
        .where(ReleaseVersion.id == version.id)
        .options(selectinload(ReleaseVersion.deploy_artifacts))
    )
    row = (await db_session.execute(stmt)).scalar_one()
    agg_dict, _ = ra.summarize_release_artifacts(row, row.deploy_artifacts)
    expected_status = agg_dict["serving_status"]

    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    assert snap.version.serving.status == expected_status
    assert snap.version.serving.status == "serving_confirmed"


@pytest.mark.asyncio
async def test_cross_surface_deploy_failed_agreement(db_session):
    """compute_system_state deploy_failed status agrees with summarize_release_artifacts."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    version = await _make_version(
        db_session, project.id, serving_status="serving_confirmed"
    )
    await _make_artifact(
        db_session,
        version.id,
        serving_status="deploy_failed",
        reason_code="halted",
    )

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models import ReleaseVersion

    stmt = (
        select(ReleaseVersion)
        .where(ReleaseVersion.id == version.id)
        .options(selectinload(ReleaseVersion.deploy_artifacts))
    )
    row = (await db_session.execute(stmt)).scalar_one()
    agg_dict, _ = ra.summarize_release_artifacts(row, row.deploy_artifacts)
    expected_status = agg_dict["serving_status"]

    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    assert snap.version.serving.status == expected_status
    assert snap.version.serving.status == "deploy_failed"


# ---------------------------------------------------------------------------
# Artifact evidence in ServingSnapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_evidence_included_in_serving_snapshot(db_session):
    """Per-artifact evidence is captured in serving.artifacts."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    version = await _make_version(db_session, project.id)
    await _make_artifact(
        db_session,
        version.id,
        artifact_kind=ra.BACKEND_REVISION_ARTIFACT_KIND,
        serving_status="deploy_failed",
        reason_code="no_new_revision",
    )

    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    artifacts = snap.version.serving.artifacts
    assert len(artifacts) >= 1
    backend_a = next(
        (a for a in artifacts if a.artifact_kind == ra.BACKEND_REVISION_ARTIFACT_KIND),
        None,
    )
    assert backend_a is not None
    assert backend_a.serving_status == "deploy_failed"
    assert backend_a.reason_code == "no_new_revision"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_to_dict_json_serializable(db_session):
    """to_dict() returns a plain dict with no unserializable types (e.g. datetime)."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    snap = await compute_system_state(db_session, project.id)
    d = snap.to_dict()
    # Must not raise TypeError
    json.dumps(d)
    assert isinstance(d, dict)
    assert isinstance(d["deploy"], dict)
    assert isinstance(d["fleet"], dict)
    assert d["version"] is None


@pytest.mark.asyncio
async def test_cross_surface_external_project_serving_agreement(db_session):
    """External-project: promotion_merge confirmed + no backend_revision row → serving_confirmed.

    Without project= threading, backend_revision scalar-projects the version's
    serving_status ("unconfirmed") and holds the aggregate back to "unconfirmed".
    With project= threaded, backend_revision is non-observable (deploy identity
    does not match this project), so only the promotion_merge artifact contributes
    to the aggregate, yielding "serving_confirmed".
    """
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    # Armed promotion: version scalar says "unconfirmed" (as it would be after
    # _record_promotion_success set it but before backend confirmation).
    version = await _make_version(db_session, project.id, serving_status="unconfirmed")

    # Promotion-merge artifact confirmed (the GitHub merge completed).
    await _make_artifact(
        db_session,
        version.id,
        artifact_kind=ra.PROMOTION_MERGE_ARTIFACT_KIND,
        serving_status="serving_confirmed",
        served_identifier="abc123",
        served_at=_utcnow(),
    )
    # Deliberately NO backend_revision artifact row — external project.

    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None

    # The aggregate (project-aware) should be serving_confirmed because only
    # the observable promotion_merge artifact contributes, and it is confirmed.
    assert snap.version.serving.status == "serving_confirmed"

    # Verify by comparing directly with the project-aware summarize call.
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models import ReleaseVersion

    stmt = (
        select(ReleaseVersion)
        .where(ReleaseVersion.id == version.id)
        .options(selectinload(ReleaseVersion.deploy_artifacts))
    )
    row = (await db_session.execute(stmt)).scalar_one()
    agg_dict, evidence_list = ra.summarize_release_artifacts(
        row, row.deploy_artifacts, project=project
    )
    assert agg_dict["serving_status"] == "serving_confirmed"
    assert snap.version.serving.status == agg_dict["serving_status"]

    # No backend_revision entry with a non-None serving_status should appear —
    # the vacuous path must run for non-observable providers.
    backend_entry = next(
        (
            e
            for e in evidence_list
            if e["artifact_kind"] == ra.BACKEND_REVISION_ARTIFACT_KIND
        ),
        None,
    )
    assert backend_entry is None or backend_entry["serving_status"] is None


@pytest.mark.asyncio
async def test_to_dict_datetime_fields_are_strings(db_session):
    """Datetime fields in to_dict are ISO strings, not datetime objects."""
    await _make_deploy_state(db_session)
    snap = await compute_system_state(db_session)
    d = snap.to_dict()
    # deploy.deployed_at should be an ISO string
    deployed_at = d["deploy"]["deployed_at"]
    assert isinstance(deployed_at, str)
    # Must be parseable as an ISO datetime
    from datetime import datetime

    parsed = datetime.fromisoformat(deployed_at.rstrip("Z"))
    assert parsed is not None


# ---------------------------------------------------------------------------
# Three-surface cross-surface agreement (acceptance criterion for truthful state)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serving_sha_uses_confirmed_artifact_identifier(db_session):
    """External-project: serving.sha comes from confirmed artifact, not the stale scalar.

    Acceptance evidence: version with a confirmed promotion_merge artifact
    (served_identifier="abc123"), no backend_revision row, and unset served_sha
    scalar yields snapshot serving.sha equal to the artifact's served_identifier.
    """
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    # served_sha scalar is None — as it would be for an external project that
    # never syncs the backend_revision serving confirmation.
    version = await _make_version(db_session, project.id, serving_status="unconfirmed")

    await _make_artifact(
        db_session,
        version.id,
        artifact_kind=ra.PROMOTION_MERGE_ARTIFACT_KIND,
        serving_status="serving_confirmed",
        served_identifier="abc123",
        served_at=_utcnow(),
    )

    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    assert snap.version.serving.status == "serving_confirmed"
    # Must use the artifact's served_identifier, not the stale/blank scalar.
    assert snap.version.serving.sha == "abc123"


@pytest.mark.asyncio
async def test_serving_sha_falls_back_when_no_evidence(db_session):
    """Fallback: version with no confirmed artifacts uses version.served_sha."""
    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    await _make_version(
        db_session,
        project.id,
        serving_status="serving_confirmed",
        served_sha="legacy-sha",
    )
    # No artifact rows — aggregate falls back to scalar.

    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    assert snap.version.serving.sha == "legacy-sha"


@pytest.mark.asyncio
async def test_all_surfaces_agree_serving_status(db_session):
    """All four surfaces agree on serving_confirmed for an external-project version.

    External-project shape: one promotion_merge artifact (serving_confirmed),
    no backend_revision artifact.  Without project= threading, the legacy scalar
    fallback would synthesize an "unconfirmed" backend_revision contribution and
    hold the aggregate back.  With project= threaded, backend_revision is
    non-observable and vacuous, so only the promotion_merge artifact contributes,
    yielding "serving_confirmed" across all four read surfaces.

    The four surfaces are:
    1. compute_system_state() canonical snapshot
    2. admin release-version serialization
    3. strategic-spawning version entry
    4. deployment_state block promoted_serving_status (via _version_serving_summary)
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models import ReleaseVersion
    from app.routers.admin_release_version import _serialize_version_history_entry
    from app.strategic_spawning import (
        _strategic_recent_version_entry,
        _version_serving_summary,
    )

    await _make_deploy_state(db_session)
    project = await _make_project(db_session)
    # Version scalar starts as "unconfirmed" (as set by _record_promotion_success
    # before backend confirmation arrives).
    version = await _make_version(db_session, project.id, serving_status="unconfirmed")

    # Promotion-merge artifact confirmed (GitHub merge completed).
    await _make_artifact(
        db_session,
        version.id,
        artifact_kind=ra.PROMOTION_MERGE_ARTIFACT_KIND,
        serving_status="serving_confirmed",
        served_identifier="sha-abc123",
        served_at=_utcnow(),
    )
    # Deliberately NO backend_revision artifact — external-project shape.

    # Reload version with its artifacts relationship eagerly loaded so the
    # admin/strategic helper functions can access version.deploy_artifacts.
    stmt = (
        select(ReleaseVersion)
        .where(ReleaseVersion.id == version.id)
        .options(selectinload(ReleaseVersion.deploy_artifacts))
    )
    version_row = (await db_session.execute(stmt)).scalar_one()

    # Surface 1: compute_system_state canonical read.
    snap = await compute_system_state(db_session, project.id)
    assert snap.version is not None
    surface1_status = snap.version.serving.status

    # Surface 2: admin release-version serialization.
    surface2_entry = await _serialize_version_history_entry(version_row, project)
    surface2_status = surface2_entry["aggregate_serving_status"]

    # Surface 3: strategic-spawning version entry.
    surface3_entry = await _strategic_recent_version_entry(
        db_session, version_row, project
    )
    surface3_status = surface3_entry["serving_status"]

    # Surface 4: deployment_state block promoted_serving_status
    # (computed the same way for the promoted version).
    surface4_aggregate, surface4_artifacts = _version_serving_summary(
        version_row, project
    )
    surface4_status = surface4_aggregate["serving_status"]

    # All four must agree and all must be "serving_confirmed".
    assert surface1_status == "serving_confirmed", (
        f"compute_system_state returned {surface1_status!r}"
    )
    assert surface2_status == "serving_confirmed", (
        f"_serialize_version_history_entry returned {surface2_status!r}"
    )
    assert surface3_status == "serving_confirmed", (
        f"_strategic_recent_version_entry returned {surface3_status!r}"
    )
    assert surface4_status == "serving_confirmed", (
        f"_version_serving_summary returned {surface4_status!r}"
    )
    assert surface1_status == surface2_status == surface3_status == surface4_status
