"""Lock-free serving-provider observation with exact short-transaction apply."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app import release_version
from app.deploy_identity import deploy_repo_identity
from app.models import Project, ReleaseDeployArtifact
from app.release_artifacts import (
    apply_serving_decision,
    artifact_is_unconfirmed,
    is_backend_revision_artifact,
    iter_release_artifact_providers,
    observable_release_artifact_providers,
    seed_backend_revision_artifact_from_version,
)
from app.release_authority_lock import lock_project_then_version
from app.release_promotion import _DEPLOY_CONFIRM_WINDOW_SECONDS

_MODULE_SIZE_GOVERNANCE_CAP: int = 500

_ARTIFACT_STATE_FIELDS = (
    "serving_status",
    "served_identifier",
    "confirmation_deadline",
    "reason_code",
    "served_at",
)


class _StrongIdentity:
    """Process-local CAS identity that retains its referent strongly."""

    __slots__ = ("value",)

    def __init__(self, value: object):
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _StrongIdentity) and self.value is other.value

    __hash__ = None


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def serving_project_authority_fingerprint(project: Project) -> str:
    return _canonical_hash(
        {
            "id": str(project.id),
            "user_id": str(project.user_id),
            "paused": bool(project.paused),
            "archived": bool(project.archived),
            "github_owner": project.github_owner,
            "github_repo": project.github_repo,
            "integration_branch": project.integration_branch,
            "promotion_target": project.promotion_target,
            "build_config": project.build_config,
        }
    )


def serving_version_authority_fingerprint(
    version: release_version.ReleaseVersion,
) -> str:
    return _canonical_hash(
        {
            column.name: getattr(version, column.name)
            for column in version.__table__.columns
        }
    )


def _artifact_snapshot(artifact: ReleaseDeployArtifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "version_id": str(artifact.version_id),
        "artifact_kind": artifact.artifact_kind,
        "serving_status": artifact.serving_status,
        "served_identifier": artifact.served_identifier,
        "confirmation_deadline": artifact.confirmation_deadline,
        "reason_code": artifact.reason_code,
        "served_at": artifact.served_at,
        "created_at": artifact.created_at,
        "updated_at": artifact.updated_at,
    }


def _artifact_baseline(artifacts: list[ReleaseDeployArtifact]) -> tuple[str, ...]:
    return tuple(
        _canonical_hash(_artifact_snapshot(artifact))
        for artifact in sorted(
            artifacts,
            key=lambda row: (str(row.artifact_kind or ""), str(row.id)),
        )
    )


def serving_artifact_authority_fingerprint(
    artifacts: list[ReleaseDeployArtifact],
) -> tuple[str, ...]:
    return _artifact_baseline(artifacts)


def _clone_version(
    version: release_version.ReleaseVersion,
) -> release_version.ReleaseVersion:
    return release_version.ReleaseVersion(
        **{
            column.name: getattr(version, column.name)
            for column in version.__table__.columns
        }
    )


def _clone_artifact(artifact: ReleaseDeployArtifact) -> ReleaseDeployArtifact:
    return ReleaseDeployArtifact(
        **{
            column.name: getattr(artifact, column.name)
            for column in artifact.__table__.columns
        }
    )


def _provider_registry_signature() -> tuple:
    """Fingerprint provider authority without invoking provider callbacks."""
    return tuple(
        (
            str(p.artifact_kind), bool(p.required), _StrongIdentity(p),
            _StrongIdentity(getattr(p.check, "__func__", p.check)),
            _StrongIdentity(getattr(p.observable, "__func__", p.observable)),
        )
        for p in iter_release_artifact_providers()
    )


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    artifact_kind: str
    serving_status: str | None
    served_identifier: str | None
    confirmation_deadline: datetime | None
    reason_code: str | None
    served_at: datetime | None


@dataclass(frozen=True, slots=True)
class ServingObservationPlan:
    version_id: uuid.UUID
    project_id: uuid.UUID
    project_authority: str
    version_authority: str
    artifact_baseline: tuple[str, ...]
    provider_signature: tuple
    deploy_identity: Any
    version_status: str
    serving_status: str | None
    desired_deadline: datetime | None
    artifacts: tuple[ArtifactObservation, ...]
    had_required_unconfirmed: bool
    mutated: bool
    backend_artifact_kind: str | None


@dataclass(slots=True)
class AppliedServingObservation:
    version: release_version.ReleaseVersion
    provider_rows: list[tuple[object, ReleaseDeployArtifact]]
    backend_artifact: ReleaseDeployArtifact | None
    mutated: bool


async def observe_release_artifacts(
    session,
    version_id: uuid.UUID,
    now: datetime,
    *,
    recheck_failed_required: bool = False,
    expected_version_status: str | None = None,
    expected_serving_statuses: frozenset[str | None] | None = None,
    backend_served_identifier: str | None = None,
    backend_served_at: datetime | None = None,
) -> ServingObservationPlan | None:
    """Run all provider checks without release-authority or artifact locks."""

    try:
        version = await session.get(
            release_version.ReleaseVersion, version_id, populate_existing=True
        )
        if version is None:
            return None
        if (
            expected_version_status is not None
            and version.status != expected_version_status
        ):
            return None
        if (
            expected_serving_statuses is not None
            and version.serving_status not in expected_serving_statuses
        ):
            return None
        project = await session.get(Project, version.project_id, populate_existing=True)
        if project is None:
            return None
        existing = (
            (
                await session.execute(
                    select(ReleaseDeployArtifact)
                    .where(ReleaseDeployArtifact.version_id == version.id)
                    .order_by(
                        ReleaseDeployArtifact.artifact_kind,
                        ReleaseDeployArtifact.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        project_authority = serving_project_authority_fingerprint(project)
        version_authority = serving_version_authority_fingerprint(version)
        artifact_baseline = _artifact_baseline(existing)
        deploy_identity = deploy_repo_identity()
        provider_signature = _provider_registry_signature()
        observable_providers = tuple(
            observable_release_artifact_providers(project, deploy_identity)
        )

        observed_version = _clone_version(version)
        if (
            observed_version.deploy_confirm_deadline is None
            and observed_version.promoted_at is not None
        ):
            observed_version.deploy_confirm_deadline = (
                observed_version.promoted_at
                + timedelta(seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS)
            )
        observed_by_kind = {
            str(row.artifact_kind): _clone_artifact(row)
            for row in existing
            if row.artifact_kind
        }
        original_by_kind = {
            str(row.artifact_kind): _artifact_snapshot(row)
            for row in existing
            if row.artifact_kind
        }
        provider_rows: list[tuple[object, ReleaseDeployArtifact]] = []
        observable_kinds: set[str] = set()
        had_required_unconfirmed = False
        backend_kind: str | None = None

        # Transient model copies are never added to ``session``. Provider DB
        # reads therefore cannot autoflush an earlier decision or retain an
        # artifact lock across the next provider's I/O.
        with session.no_autoflush:
            for provider in observable_providers:
                kind = str(provider.artifact_kind)
                artifact = observed_by_kind.get(kind)
                if artifact is None:
                    artifact = ReleaseDeployArtifact(
                        version_id=observed_version.id,
                        artifact_kind=kind,
                        confirmation_deadline=(
                            observed_version.deploy_confirm_deadline
                        ),
                        **(
                            {}
                            if is_backend_revision_artifact(kind)
                            else {"serving_status": "unconfirmed"}
                        ),
                    )
                    if is_backend_revision_artifact(kind):
                        seed_backend_revision_artifact_from_version(
                            observed_version, artifact
                        )
                    observed_by_kind[kind] = artifact
                elif (
                    artifact.confirmation_deadline is None
                    and observed_version.deploy_confirm_deadline is not None
                ):
                    artifact.confirmation_deadline = (
                        observed_version.deploy_confirm_deadline
                    )
                if is_backend_revision_artifact(kind):
                    seed_backend_revision_artifact_from_version(
                        observed_version, artifact
                    )
                    backend_kind = kind
                    if backend_served_identifier:
                        apply_serving_decision(
                            artifact,
                            (
                                "serving_confirmed",
                                backend_served_identifier,
                                backend_served_at or now,
                                None,
                            ),
                            now=now,
                        )
                if provider.required and artifact_is_unconfirmed(artifact):
                    had_required_unconfirmed = True
                should_check = artifact_is_unconfirmed(artifact) or (
                    recheck_failed_required
                    and provider.required
                    and artifact.serving_status
                    in ("deploy_failed", "confirmation_timed_out")
                )
                if should_check:
                    outcome = await provider.check(
                        session, observed_version, artifact, now
                    )
                    if outcome is not None:
                        apply_serving_decision(artifact, outcome, now=now)
                provider_rows.append((provider, artifact))
                observable_kinds.add(kind)

            required_map = {
                str(provider.artifact_kind): provider
                for provider in iter_release_artifact_providers()
                if provider.required
            }
            now_naive = now.replace(tzinfo=None) if now.tzinfo else now
            for kind in sorted(set(required_map) - observable_kinds):
                artifact = observed_by_kind.get(kind)
                if artifact is None:
                    continue
                provider = required_map[kind]
                if artifact_is_unconfirmed(artifact):
                    had_required_unconfirmed = True
                    deadline = (
                        artifact.confirmation_deadline
                        or observed_version.deploy_confirm_deadline
                    )
                    deadline_naive = (
                        deadline.replace(tzinfo=None)
                        if deadline is not None and deadline.tzinfo
                        else deadline
                    )
                    if deadline_naive is not None and now_naive >= deadline_naive:
                        apply_serving_decision(
                            artifact,
                            (
                                "confirmation_timed_out",
                                "provider_unobservable",
                                None,
                            ),
                            now=now,
                        )
                provider_rows.append((provider, artifact))

        observations = tuple(
            ArtifactObservation(
                artifact_kind=str(artifact.artifact_kind),
                **{
                    field: getattr(artifact, field)
                    for field in _ARTIFACT_STATE_FIELDS
                },
            )
            for _provider, artifact in provider_rows
        )
        mutated = (
            observed_version.deploy_confirm_deadline
            != version.deploy_confirm_deadline
            or any(
                kind not in original_by_kind
                or any(
                    original_by_kind[kind].get(field) != getattr(artifact, field)
                    for field in _ARTIFACT_STATE_FIELDS
                )
                for kind, artifact in observed_by_kind.items()
            )
        )
        return ServingObservationPlan(
            version_id=version.id,
            project_id=version.project_id,
            project_authority=project_authority,
            version_authority=version_authority,
            artifact_baseline=artifact_baseline,
            provider_signature=provider_signature,
            deploy_identity=deploy_identity,
            version_status=version.status,
            serving_status=version.serving_status,
            desired_deadline=observed_version.deploy_confirm_deadline,
            artifacts=observations,
            had_required_unconfirmed=had_required_unconfirmed,
            mutated=mutated,
            backend_artifact_kind=backend_kind,
        )
    finally:
        await session.rollback()


async def apply_serving_observation(
    session,
    plan: ServingObservationPlan,
) -> AppliedServingObservation | None:
    """CAS one provider plan under Project -> Version -> artifact authority."""

    version = await lock_project_then_version(session, plan.version_id)
    if version is None:
        await session.rollback()
        return None
    project = await session.get(Project, plan.project_id, populate_existing=True)
    artifacts = (
        (
            await session.execute(
                select(ReleaseDeployArtifact)
                .where(ReleaseDeployArtifact.version_id == plan.version_id)
                .order_by(
                    ReleaseDeployArtifact.artifact_kind,
                    ReleaseDeployArtifact.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    current_identity = deploy_repo_identity()
    if (
        project is None
        or serving_project_authority_fingerprint(project) != plan.project_authority
        or serving_version_authority_fingerprint(version)
        != plan.version_authority
        or _artifact_baseline(artifacts) != plan.artifact_baseline
        or current_identity != plan.deploy_identity
        or _provider_registry_signature() != plan.provider_signature
    ):
        await session.rollback()
        return None

    version.deploy_confirm_deadline = plan.desired_deadline
    session.add(version)
    by_kind = {
        str(row.artifact_kind): row for row in artifacts if row.artifact_kind
    }
    for desired in plan.artifacts:
        artifact = by_kind.get(desired.artifact_kind)
        if artifact is None:
            artifact = ReleaseDeployArtifact(
                version_id=plan.version_id,
                artifact_kind=desired.artifact_kind,
            )
            by_kind[desired.artifact_kind] = artifact
        for field in _ARTIFACT_STATE_FIELDS:
            setattr(artifact, field, getattr(desired, field))
        session.add(artifact)

    provider_map = {
        str(provider.artifact_kind): provider
        for provider in iter_release_artifact_providers()
    }
    provider_rows = [
        (provider_map[desired.artifact_kind], by_kind[desired.artifact_kind])
        for desired in plan.artifacts
    ]
    backend_artifact = (
        by_kind.get(plan.backend_artifact_kind)
        if plan.backend_artifact_kind is not None
        else None
    )
    return AppliedServingObservation(
        version=version,
        provider_rows=provider_rows,
        backend_artifact=backend_artifact,
        mutated=plan.mutated,
    )
