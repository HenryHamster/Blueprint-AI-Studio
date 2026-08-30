"""Focused authority/CAS tests for unlocked serving-provider observation."""

import gc
import weakref
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import release_artifacts as artifact_registry
from app.database import resolve_async_engine
from app.models import Project, ReleaseDeployArtifact, ReleaseVersion
from app.release_artifacts import ReleaseArtifactProvider
from app.release_serving_observation import (
    _StrongIdentity,
    apply_serving_observation,
    observe_release_artifacts,
)

_SHA = "a" * 40


class _Referent:
    pass


def test_strong_identity_retains_referent_and_uses_object_identity():
    referent = _Referent()
    retained = weakref.ref(referent)
    identity = _StrongIdentity(referent)
    del referent
    gc.collect()

    assert retained() is not None
    assert identity == _StrongIdentity(retained())
    assert identity != _StrongIdentity(_Referent())


async def _promoted_version(db_session):
    project = Project(name="Serving-CAS", user_id="serving-cas-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = ReleaseVersion(
        project_id=project.id,
        version_number=1,
        status="promoted",
        promoted_sha=_SHA,
        promoted_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        serving_status="unconfirmed",
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    return project, version


def _synthetic_provider(check, *, observable=None):
    kwargs = {} if observable is None else {"observable": observable}
    return ReleaseArtifactProvider(
        artifact_kind="synthetic_required",
        check=check,
        required=True,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_observe_then_exact_apply_does_not_replay_provider(
    db_session, mocker
):
    _project, version = await _promoted_version(db_session)
    version_id = version.id
    check = mocker.AsyncMock(
        return_value=("serving_confirmed", "synthetic-digest", None)
    )
    callback_state = {"armed": False, "calls": 0}

    def _observable(_project, _identity):
        callback_state["calls"] += 1
        if callback_state["armed"]:
            raise AssertionError("observable callback ran during exact apply")
        return True

    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {
            "synthetic_required": _synthetic_provider(
                check, observable=_observable
            )
        },
        clear=True,
    )

    plan = await observe_release_artifacts(db_session, version_id, datetime.now(timezone.utc))
    assert plan is not None
    check.assert_awaited_once()
    observable_calls = callback_state["calls"]
    callback_state["armed"] = True

    applied = await apply_serving_observation(db_session, plan)
    assert applied is not None
    check.assert_awaited_once()
    assert callback_state["calls"] == observable_calls
    await db_session.commit()
    artifact = (
        await db_session.execute(
            select(ReleaseDeployArtifact).where(
                ReleaseDeployArtifact.version_id == version_id,
                ReleaseDeployArtifact.artifact_kind == "synthetic_required",
            )
        )
    ).scalar_one()
    assert artifact.serving_status == "serving_confirmed"
    assert artifact.served_identifier == "synthetic-digest"


@pytest.mark.asyncio
async def test_real_provider_check_runs_without_authority_row_locks(
    db_session, mocker
):
    project, version = await _promoted_version(db_session)
    project_id = project.id
    version_id = version.id
    artifact = ReleaseDeployArtifact(
        version_id=version_id,
        artifact_kind="synthetic_required",
        serving_status="unconfirmed",
    )
    db_session.add(artifact)
    await db_session.commit()
    await db_session.refresh(artifact)
    artifact_id = artifact.id
    probe_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    probes = []

    async def _check(_session, _version, _artifact, now):
        async with probe_factory() as probe:
            for model, row_id in (
                (Project, project_id),
                (ReleaseVersion, version_id),
                (ReleaseDeployArtifact, artifact_id),
            ):
                await probe.execute(
                    select(model)
                    .where(model.id == row_id)
                    .with_for_update(nowait=True)
                )
                probes.append(model.__name__)
            await probe.rollback()
        return ("serving_confirmed", "synthetic-digest", now)

    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {"synthetic_required": _synthetic_provider(_check)},
        clear=True,
    )

    plan = await observe_release_artifacts(
        db_session, version_id, datetime.now(timezone.utc)
    )

    assert plan is not None
    assert probes == ["Project", "ReleaseVersion", "ReleaseDeployArtifact"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift", ["project", "version", "artifact", "deploy_identity", "provider"]
)
async def test_exact_apply_rejects_authority_drift(db_session, mocker, drift):
    project, version = await _promoted_version(db_session)
    project_id = project.id
    version_id = version.id
    check = mocker.AsyncMock(
        return_value=("serving_confirmed", "synthetic-digest", None)
    )
    mocker.patch.dict(
        artifact_registry._ARTIFACT_PROVIDERS,
        {"synthetic_required": _synthetic_provider(check)},
        clear=True,
    )
    plan = await observe_release_artifacts(db_session, version_id, datetime.now(timezone.utc))
    assert plan is not None

    if drift == "project":
        current_project = await db_session.get(Project, project_id)
        current_project.build_config = {"drift": True}
        db_session.add(current_project)
        await db_session.commit()
    elif drift == "version":
        current_version = await db_session.get(ReleaseVersion, version_id)
        current_version.goal = "changed after observation"
        db_session.add(current_version)
        await db_session.commit()
    elif drift == "artifact":
        db_session.add(
            ReleaseDeployArtifact(
                version_id=version_id,
                artifact_kind="concurrent_artifact",
                serving_status="unconfirmed",
            )
        )
        await db_session.commit()
    elif drift == "deploy_identity":
        mocker.patch(
            "app.release_serving_observation.deploy_repo_identity",
            return_value=object(),
        )
    else:
        artifact_registry._ARTIFACT_PROVIDERS["synthetic_required"] = (
            _synthetic_provider(check)
        )

    assert await apply_serving_observation(db_session, plan) is None
    synthetic = (
        await db_session.execute(
            select(ReleaseDeployArtifact).where(
                ReleaseDeployArtifact.version_id == version_id,
                ReleaseDeployArtifact.artifact_kind == "synthetic_required",
            )
        )
    ).scalar_one_or_none()
    assert synthetic is None
