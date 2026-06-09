"""Serving-check registry and helpers for release deploy artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeploymentState, ReleaseDeployArtifact, ReleaseVersion

BACKEND_REVISION_ARTIFACT_KIND = "backend_revision"
AGENT_IMAGE_ARTIFACT_KIND = "agent_image"

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


@dataclass(frozen=True)
class ReleaseArtifactEvidence:
    artifact_kind: str
    required: bool
    serving_status: str | None
    served_identifier: str | None
    served_at: datetime | None
    reason_code: str | None


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
    artifacts: list[
        tuple[ReleaseArtifactProvider, ReleaseDeployArtifact | ReleaseArtifactEvidence]
    ],
) -> tuple[str, str | None]:
    """Aggregate the required-artifact states for a version."""

    required = [
        (provider, artifact) for provider, artifact in artifacts if provider.required
    ]
    if not required:
        return ("serving_confirmed", None)
    # Precedence: an ACTIVE failure dominates everything; while anything is still
    # genuinely pending we keep waiting (unconfirmed); only once nothing is
    # pending does a TIMEOUT surface; all-confirmed is the success case.
    for _provider, artifact in required:
        if artifact.serving_status == "deploy_failed":
            return ("deploy_failed", artifact.reason_code)
    for _provider, artifact in required:
        if (artifact.serving_status or "unconfirmed") == "unconfirmed":
            return ("unconfirmed", None)
    for _provider, artifact in required:
        if artifact.serving_status == "confirmation_timed_out":
            return ("confirmation_timed_out", artifact.reason_code)
    if all(
        artifact.serving_status == "serving_confirmed"
        for _provider, artifact in required
    ):
        return ("serving_confirmed", None)
    return ("unconfirmed", None)


def summarize_release_artifacts(
    version: ReleaseVersion,
    artifacts: Iterable[ReleaseDeployArtifact] | None,
) -> tuple[dict[str, str | None], list[dict[str, Any]]]:
    """Summarize aggregate serving truth plus per-provider evidence.

    The aggregate status follows the required-artifact registry semantics:

    * ``deploy_failed`` iff any required artifact failed,
    * ``serving_confirmed`` iff every required artifact confirmed serving,
    * otherwise ``unconfirmed`` while any required artifact is pending.

    For backward compatibility, a missing backend artifact row falls back to the
    legacy ``ReleaseVersion`` scalar projection. When no artifact evidence exists
    at all (for example, an open version that has never promoted), the helper
    preserves the scalar truth instead of inventing a serving state.
    """

    artifact_by_kind = {
        artifact.artifact_kind: artifact
        for artifact in (artifacts or [])
        if artifact.artifact_kind
    }
    evidence_rows: list[ReleaseArtifactEvidence] = []
    aggregate_rows: list[tuple[ReleaseArtifactProvider, ReleaseArtifactEvidence]] = []

    for provider in iter_release_artifact_providers():
        artifact = artifact_by_kind.get(provider.artifact_kind)
        if artifact is not None:
            evidence = ReleaseArtifactEvidence(
                artifact_kind=provider.artifact_kind,
                required=provider.required,
                serving_status=artifact.serving_status,
                served_identifier=artifact.served_identifier,
                served_at=artifact.served_at,
                reason_code=artifact.reason_code,
            )
        elif is_backend_revision_artifact(provider.artifact_kind):
            evidence = ReleaseArtifactEvidence(
                artifact_kind=provider.artifact_kind,
                required=provider.required,
                serving_status=(
                    version.serving_status
                    if version.serving_status is not None
                    else ("unconfirmed" if version.status == "promoted" else None)
                ),
                served_identifier=version.served_sha,
                served_at=version.served_at,
                reason_code=version.deploy_failure_reason,
            )
        else:
            # No artifact row exists for this provider. A required non-backend
            # provider whose row was never created must NOT strand the aggregate
            # as "unconfirmed": current self-repo promotions create every
            # required row synchronously in _record_promotion_success, so an
            # absent row means the version predates the provider (legacy /
            # backward-compat) or is a project the provider does not apply to
            # (e.g. a non-self-repo version, which gets no agent_image row).
            # Stranding those to "unconfirmed" would flip every such promoted
            # version to a permanent false-negative serving state in the
            # lifecycle/history and crisis-detection reads. Treat a missing row
            # as vacuous (None) so the aggregate is computed only over the rows
            # that actually exist plus the backend_revision legacy scalar
            # fallback above; live promotions stay gated because their row
            # exists and reports its real serving_status.
            evidence = ReleaseArtifactEvidence(
                artifact_kind=provider.artifact_kind,
                required=provider.required,
                serving_status=None,
                served_identifier=None,
                served_at=None,
                reason_code=None,
            )
        evidence_rows.append(evidence)
        if evidence.serving_status is not None:
            aggregate_rows.append((provider, evidence))

    if aggregate_rows:
        aggregate_status, aggregate_reason = aggregate_required_artifact_status(
            aggregate_rows
        )
    else:
        aggregate_status = version.serving_status
        aggregate_reason = version.deploy_failure_reason

    return (
        {
            "serving_status": aggregate_status,
            "reason_code": aggregate_reason,
        },
        [
            {
                "artifact_kind": evidence.artifact_kind,
                "required": evidence.required,
                "serving_status": evidence.serving_status,
                "served_identifier": evidence.served_identifier,
                "served_at": (
                    evidence.served_at.isoformat() if evidence.served_at else None
                ),
                "reason_code": evidence.reason_code,
            }
            for evidence in evidence_rows
        ],
    )


def summarize_required_artifact_gap(
    artifact_evidence: Iterable[dict[str, Any]] | None,
) -> str | None:
    """Return a compact description of pending/failed required artifacts."""

    failed_parts: list[str] = []
    timed_out_parts: list[str] = []
    pending_parts: list[str] = []
    for artifact in artifact_evidence or []:
        if not artifact.get("required"):
            continue
        artifact_kind = str(artifact.get("artifact_kind") or "?")
        status = artifact.get("serving_status")
        reason = artifact.get("reason_code")
        if status == "deploy_failed":
            failed_parts.append(
                f"{artifact_kind} (reason={reason})" if reason else artifact_kind
            )
        elif status == "confirmation_timed_out":
            timed_out_parts.append(
                f"{artifact_kind} (reason={reason})" if reason else artifact_kind
            )
        elif status != "serving_confirmed":
            pending_parts.append(artifact_kind)
    if failed_parts:
        return "required artifacts failed: " + ", ".join(failed_parts)
    if timed_out_parts:
        return "required artifacts confirmation timed out: " + ", ".join(
            timed_out_parts
        )
    if pending_parts:
        return "waiting on required artifacts: " + ", ".join(pending_parts)
    return None


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


def _previously_deployed_sha(
    ds: DeploymentState | None,
    promoted_sha: str,
) -> str | None:
    """Best-effort: the self-repo SHA serving *before* this promotion.

    The agent-image bake diff is ``previously-deployed SHA ... promoted_sha``.
    When the post-promotion backend revision has already advanced
    ``current_sha`` to the promoted SHA, the pre-promotion serving commit is the
    recorded ``prior_deploy_sha``; otherwise ``current_sha`` still holds it.
    Returns ``None`` when neither is observable (diff base indeterminate).
    """
    if ds is None:
        return None
    current = ds.current_sha or None
    if current and current == promoted_sha:
        return ds.prior_deploy_sha or None
    return current


async def _check_agent_image_serving(
    session: AsyncSession,
    version: ReleaseVersion,
    artifact: ReleaseDeployArtifact,
    now: datetime,
) -> ServingDecision | None:
    """Serving check for the agent VM image artifact (self-repo only).

    A self-repo promotion whose diff touches the agent VM image bake paths
    (``agent/`` or ``infra/packer/``) is only fully served once a freshly baked
    agent image is rolled out and the running fleet is observed serving the
    promoted source SHA.  This gate makes that second dimension a REQUIRED
    serving-verified artifact:

      * Non-bake promotion (diff did NOT touch the bake paths) — resolve to
        ``serving_confirmed`` IMMEDIATELY (served identifier = promoted_sha) so
        the version is never stranded waiting on a bake that will never run.
      * Bake promotion (diff touched the bake paths) — ``serving_confirmed``
        only once the fleet INDEPENDENTLY reports ``agent_source_sha`` matching
        ``version.promoted_sha``; until then return ``None`` (not-yet) so the
        version stays unconfirmed within the existing confirmation window.
      * Indeterminate (non-self-repo, no GitHub creds, the rebake lookup errored,
        the diff base is unobservable, or no fleet VM has reported a source SHA
        yet) — treat as NOT-YET-CONFIRMED (return ``None``), never
        ``deploy_failed``.  The version then resolves within the existing
        confirmation/recovery windows exactly as the backend_revision artifact
        does.

    Lazy imports avoid a load-time cycle (``release_promotion`` imports this
    module), mirroring ``_check_backend_revision_serving``.
    """
    from app import compute, github_refs
    from app.models import Project
    from app.release_promotion import _resolve_github_with_session

    promoted_sha = version.promoted_sha
    if not promoted_sha:
        return None

    project = await session.get(Project, version.project_id)
    if project is None or not getattr(project, "is_self_repo", False):
        # The agent VM image gate is scoped to the self-repo — only the self
        # deploy runs the agent fleet this dimension reasons about.  A
        # non-self-repo never gets an agent_image artifact at promotion time, so
        # this is reached only if the generic refresh lazily created one.  There
        # is no agent fleet to gate on, so the dimension is VACUOUSLY served:
        # confirm immediately rather than strand the artifact unconfirmed (the
        # same "never strand a bake that will never run" principle as the
        # no-bake-path branch below).
        return ("serving_confirmed", promoted_sha, now)

    ds = await session.get(DeploymentState, 1)
    base_sha = _previously_deployed_sha(ds, promoted_sha)
    if not base_sha:
        # Diff base unobservable → indeterminate → not-yet-confirmed.
        return None

    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        return None
    token, owner, repo = resolved

    rebake = await github_refs.agent_image_rebake_expected(
        token,
        owner,
        repo,
        base_sha,
        promoted_sha,
        project_id=str(version.project_id),
    )
    if rebake is None:
        # API/ref error → indeterminate → not-yet-confirmed.
        return None
    if rebake is False:
        # No bake-path change → no agent rebake required → confirm immediately so
        # the aggregate tracks the backend revision alone.
        return ("serving_confirmed", promoted_sha, now)

    # Bake expected: confirm only once the fleet is independently observed
    # serving the promoted SHA (the freshly baked image is live and running).
    if await compute.fleet_serving_source_sha(session, promoted_sha):
        return ("serving_confirmed", promoted_sha, now)
    return None


# Self-repo agent VM image serving gate.  Registered REQUIRED so a bake-path
# promotion stays NOT fully serving until the fleet serves the promoted SHA.
# Guarded against double-registration: a sibling task may also register an
# agent_image provider and the registry is keyed by artifact_kind — register
# exactly once here so neither call site clobbers the other's semantics.
if AGENT_IMAGE_ARTIFACT_KIND not in _ARTIFACT_PROVIDERS:
    register_release_artifact_provider(
        AGENT_IMAGE_ARTIFACT_KIND,
        _check_agent_image_serving,
        required=True,
    )
