"""Serving-check registry and helpers for release deploy artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeploymentState, ReleaseDeployArtifact, ReleaseVersion

BACKEND_REVISION_ARTIFACT_KIND = "backend_revision"

ServingDecision = tuple[str, str | None, datetime | None]
ServingCheck = Callable[
    [AsyncSession, ReleaseVersion, ReleaseDeployArtifact, datetime],
    Awaitable[ServingDecision | None],
]


@dataclass(frozen=True)
class ReleaseArtifactProvider:
    artifact_kind: str
    check: ServingCheck
    required: bool = True


_ARTIFACT_PROVIDERS: dict[str, ReleaseArtifactProvider] = {}


def register_release_artifact_provider(
    artifact_kind: str,
    check: ServingCheck,
    *,
    required: bool = True,
) -> None:
    _ARTIFACT_PROVIDERS[artifact_kind] = ReleaseArtifactProvider(
        artifact_kind=artifact_kind,
        check=check,
        required=required,
    )


def iter_release_artifact_providers() -> tuple[ReleaseArtifactProvider, ...]:
    return tuple(_ARTIFACT_PROVIDERS.values())


def required_release_artifact_kinds() -> tuple[str, ...]:
    return tuple(
        provider.artifact_kind
        for provider in _ARTIFACT_PROVIDERS.values()
        if provider.required
    )


def is_backend_revision_artifact(artifact_kind: str) -> bool:
    return artifact_kind == BACKEND_REVISION_ARTIFACT_KIND


def apply_serving_decision(
    artifact: ReleaseDeployArtifact,
    decision: ServingDecision,
    *,
    now: datetime,
) -> bool:
    """Persist a provider decision onto an artifact row."""

    status, identity_or_reason, served_at = decision
    changed = False

    if artifact.serving_status != status:
        artifact.serving_status = status
        changed = True

    if status == "serving_confirmed":
        if artifact.served_identifier != identity_or_reason:
            artifact.served_identifier = identity_or_reason
            changed = True
        new_served_at = served_at or now
        if artifact.served_at != new_served_at:
            artifact.served_at = new_served_at
            changed = True
        if artifact.reason_code is not None:
            artifact.reason_code = None
            changed = True
        return changed

    if artifact.reason_code != identity_or_reason:
        artifact.reason_code = identity_or_reason
        changed = True
    if artifact.served_identifier is not None:
        artifact.served_identifier = None
        changed = True
    return changed


def artifact_is_unconfirmed(artifact: ReleaseDeployArtifact | None) -> bool:
    if artifact is None:
        return True
    return (artifact.serving_status or "unconfirmed") == "unconfirmed"


def aggregate_required_artifact_status(
    artifacts: list[tuple[ReleaseArtifactProvider, ReleaseDeployArtifact]],
) -> tuple[str, str | None]:
    """Aggregate the required-artifact states for a version."""

    required = [
        (provider, artifact) for provider, artifact in artifacts if provider.required
    ]
    if not required:
        return ("serving_confirmed", None)
    for _provider, artifact in required:
        if artifact.serving_status == "deploy_failed":
            return ("deploy_failed", artifact.reason_code)
    if all(
        artifact.serving_status == "serving_confirmed"
        for _provider, artifact in required
    ):
        return ("serving_confirmed", None)
    return ("unconfirmed", None)


def project_backend_revision_artifact(
    version: ReleaseVersion,
    artifact: ReleaseDeployArtifact,
) -> bool:
    """Mirror the backend-revision artifact onto the legacy ReleaseVersion scalars."""

    changed = False
    projected_status = artifact.serving_status or "unconfirmed"
    if version.serving_status != projected_status:
        version.serving_status = projected_status
        changed = True
    if version.served_sha != artifact.served_identifier:
        version.served_sha = artifact.served_identifier
        changed = True
    if version.served_at != artifact.served_at:
        version.served_at = artifact.served_at
        changed = True
    if version.deploy_confirm_deadline != artifact.confirmation_deadline:
        version.deploy_confirm_deadline = artifact.confirmation_deadline
        changed = True
    if version.deploy_failure_reason != artifact.reason_code:
        version.deploy_failure_reason = artifact.reason_code
        changed = True
    return changed


def seed_backend_revision_artifact_from_version(
    version: ReleaseVersion,
    artifact: ReleaseDeployArtifact,
) -> bool:
    """Backfill the backend artifact from the legacy scalar projection when needed."""

    changed = False
    if artifact.serving_status is None and version.serving_status is not None:
        artifact.serving_status = version.serving_status
        changed = True
    if artifact.served_identifier is None and version.served_sha is not None:
        artifact.served_identifier = version.served_sha
        changed = True
    if artifact.served_at is None and version.served_at is not None:
        artifact.served_at = version.served_at
        changed = True
    if (
        artifact.confirmation_deadline is None
        and version.deploy_confirm_deadline is not None
    ):
        artifact.confirmation_deadline = version.deploy_confirm_deadline
        changed = True
    if artifact.reason_code is None and version.deploy_failure_reason is not None:
        artifact.reason_code = version.deploy_failure_reason
        changed = True
    return changed


async def get_or_create_release_deploy_artifact(
    session: AsyncSession,
    version: ReleaseVersion,
    artifact_kind: str,
    *,
    confirmation_deadline: datetime | None = None,
) -> tuple[ReleaseDeployArtifact, bool]:
    artifact = (
        (
            await session.execute(
                select(ReleaseDeployArtifact).where(
                    ReleaseDeployArtifact.version_id == version.id,
                    ReleaseDeployArtifact.artifact_kind == artifact_kind,
                )
            )
        )
        .scalars()
        .first()
    )
    created = False
    if artifact is None:
        if is_backend_revision_artifact(artifact_kind):
            artifact = ReleaseDeployArtifact(
                version_id=version.id,
                artifact_kind=artifact_kind,
                confirmation_deadline=confirmation_deadline,
            )
            seed_backend_revision_artifact_from_version(version, artifact)
        else:
            artifact = ReleaseDeployArtifact(
                version_id=version.id,
                artifact_kind=artifact_kind,
                serving_status="unconfirmed",
                confirmation_deadline=confirmation_deadline,
            )
        session.add(artifact)
        created = True
    elif artifact.confirmation_deadline is None and confirmation_deadline is not None:
        artifact.confirmation_deadline = confirmation_deadline
        session.add(artifact)
    if is_backend_revision_artifact(artifact_kind):
        if seed_backend_revision_artifact_from_version(version, artifact):
            session.add(artifact)
    return artifact, created


async def _check_backend_revision_serving(
    session: AsyncSession,
    version: ReleaseVersion,
    artifact: ReleaseDeployArtifact,
    now: datetime,
) -> ServingDecision | None:
    """Adapt the existing deploy-health classifier into the provider registry."""

    from app.release_reconciliation import _classify_serving_outcome

    ds = await session.get(DeploymentState, 1)
    return _classify_serving_outcome(
        ds,
        version.promoted_at,
        version.promoted_sha,
        artifact.confirmation_deadline or version.deploy_confirm_deadline,
        now,
    )


register_release_artifact_provider(
    BACKEND_REVISION_ARTIFACT_KIND,
    _check_backend_revision_serving,
    required=True,
)
