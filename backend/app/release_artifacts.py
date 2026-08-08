"""Serving-check registry and helpers for release deploy artifacts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deploy_identity import (
    DeployIdentity,
    deploy_repo_identity,
    project_matches_deploy_identity,
)
from app.models import (
    DeploymentState,
    Project,
    ReleaseDeployArtifact,
    ReleaseVersion,
)

logger = logging.getLogger(__name__)

BACKEND_REVISION_ARTIFACT_KIND = "backend_revision"
AGENT_IMAGE_ARTIFACT_KIND = "agent_image"
PROMOTION_MERGE_ARTIFACT_KIND = "promotion_merge"

# Distinct reason for a serving_confirmed resolution where the OBSERVED live SHA
# is a strict DESCENDANT of the promoted SHA: the target branch legitimately
# advanced past the promoted commit before confirmation resolved.  The promoted
# code IS serving (contained in what is live) — supersession, not a failure.
SUPERSEDED_BY_DESCENDANT_REASON = "superseded_by_descendant"

# A serving decision is ``(status, identifier_or_reason, served_at)``.  An
# OPTIONAL trailing fourth element carries a ``reason_code`` annotation for
# CONFIRMED decisions (e.g. ``superseded_by_descendant``); 3-tuple confirmed
# decisions keep the historical behavior of clearing the reason code.
ServingDecision = (
    tuple[str, str | None, datetime | None]
    | tuple[str, str | None, datetime | None, str | None]
)
ServingCheck = Callable[
    [AsyncSession, ReleaseVersion, ReleaseDeployArtifact, datetime],
    Awaitable[ServingDecision | None],
]

# Per-project observability predicate: given a project and the resolved
# deploy-level identity (``(owner, repo)`` or ``None``), is this provider's
# serving signal READABLE for that project?  A provider that is not observable
# is never armed at promotion and never swept — so it can never strand a
# version waiting on a signal nothing can read.
ObservabilityPredicate = Callable[[Project | None, DeployIdentity], bool]


def _observable_for_any_github_project(
    project: Project | None,
    _identity: DeployIdentity,
) -> bool:
    """Merge-level observability: any project with an authorized GitHub repo.

    The promotion target branch is reachable through the project's own GitHub
    credentials, so the merge-persisted verdict is available for every GitHub
    project regardless of the deploy identity.
    """
    if project is None:
        return False
    return bool(
        (getattr(project, "github_owner", None) or "")
        and (getattr(project, "github_repo", None) or "")
    )


def _observable_for_deploy_identity(
    project: Project | None,
    identity: DeployIdentity,
) -> bool:
    """Deploy-level observability: the project IS the repo this deploy serves."""

    return project_matches_deploy_identity(project, identity)


@dataclass(frozen=True)
class ReleaseArtifactProvider:
    artifact_kind: str
    check: ServingCheck
    required: bool = True
    # Default: observable everywhere (a provider that does not opt into a
    # narrower predicate is treated as universally readable).
    observable: ObservabilityPredicate = lambda _project, _identity: True


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
    observable: ObservabilityPredicate | None = None,
) -> None:
    _ARTIFACT_PROVIDERS[artifact_kind] = ReleaseArtifactProvider(
        artifact_kind=artifact_kind,
        check=check,
        required=required,
        **({"observable": observable} if observable is not None else {}),
    )


def iter_release_artifact_providers() -> tuple[ReleaseArtifactProvider, ...]:
    return tuple(_ARTIFACT_PROVIDERS.values())


def observable_release_artifact_providers(
    project: Project | None,
    identity: DeployIdentity = None,
) -> tuple[ReleaseArtifactProvider, ...]:
    """Providers whose serving signal is READABLE for *project*.

    ``identity`` (the resolved deploy-level ``(owner, repo)``) may be passed in
    to resolve it once per sweep; left ``None`` it is resolved here.  This is
    the single source of truth for which artifact rows a version gets armed and
    swept with: a provider absent from this set is never created/checked for the
    project, so a deploy-level provider can never strand a non-deploy project's
    promotion waiting on a Cloud Run signal that does not exist for it.
    """
    if identity is None:
        identity = deploy_repo_identity()
    return tuple(
        provider
        for provider in _ARTIFACT_PROVIDERS.values()
        if provider.observable(project, identity)
    )


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

    status, identity_or_reason, served_at = decision[0], decision[1], decision[2]
    # Optional trailing reason_code for CONFIRMED decisions (e.g.
    # ``superseded_by_descendant``).  Absent (the 3-tuple shape every
    # pre-existing decision uses) a confirmation clears the reason code.
    confirmed_reason = decision[3] if len(decision) > 3 else None
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
        if artifact.reason_code != confirmed_reason:
            artifact.reason_code = confirmed_reason
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


# ``confirmation_timed_out`` reason codes that name no specific unsatisfied
# gate.  ``no_new_revision`` is the backend's low-information backstop ("the
# serving SHA just couldn't be confirmed in time") — not evidence of the actual
# blocker.  When several required artifacts time out together, the reported
# reason must reflect the real gate, not registry-first ordering
# (backend_revision -> agent_image -> promotion_merge), so a stale backend
# ``no_new_revision`` cannot mask a concrete gate like a fleet that never
# served the promoted SHA.
_TIMED_OUT_BACKSTOP_REASONS = frozenset({"no_new_revision"})

# ``no_new_revision`` substantiation reasons: when the backend-revision provider
# times out without ever observing a new revision serving the promoted SHA, it
# consults the deploy workflow (``deploy.yml``) runs for that SHA and names the
# specific gate that kept the promotion from shipping.  These are CONCRETE gates
# (never-dispatched / workflow-failed / succeeded-but-stale / still-running), so
# they are deliberately NOT in ``_TIMED_OUT_BACKSTOP_REASONS`` and surface over
# the low-information backstop in the aggregate.
DEPLOY_WORKFLOW_NEVER_DISPATCHED_REASON = "deploy_workflow_never_dispatched"
DEPLOY_WORKFLOW_FAILED_REASON = "deploy_workflow_failed"
DEPLOY_WORKFLOW_SUCCEEDED_STALE_REVISION_REASON = (
    "deploy_workflow_succeeded_stale_revision"
)
DEPLOY_WORKFLOW_IN_PROGRESS_REASON = "deploy_workflow_in_progress"


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
    timed_out = [
        artifact
        for _provider, artifact in required
        if artifact.serving_status == "confirmation_timed_out"
    ]
    if timed_out:
        # Report the real gate: prefer a timed-out artifact whose reason is not
        # the no_new_revision backstop, so a registry-first backend "no new
        # revision" reading cannot mask a concrete blocker.  Fall back to the
        # first when every timed-out reason is a backstop (no signal to prefer).
        primary = next(
            (
                artifact.reason_code
                for artifact in timed_out
                if artifact.reason_code not in _TIMED_OUT_BACKSTOP_REASONS
            ),
            timed_out[0].reason_code,
        )
        return ("confirmation_timed_out", primary)
    if all(
        artifact.serving_status == "serving_confirmed"
        for _provider, artifact in required
    ):
        return ("serving_confirmed", None)
    return ("unconfirmed", None)


def summarize_release_artifacts(
    version: ReleaseVersion,
    artifacts: Iterable[ReleaseDeployArtifact] | None,
    *,
    project: Project | None = None,
    identity: DeployIdentity = None,
) -> tuple[dict[str, str | None], list[dict[str, Any]]]:
    """Summarize aggregate serving truth plus per-provider evidence.

    The aggregate status follows the required-artifact registry semantics:

    * ``deploy_failed`` iff any required artifact failed,
    * ``serving_confirmed`` iff every required artifact confirmed serving,
    * otherwise ``unconfirmed`` while any required artifact is pending.

    For backward compatibility, a missing backend artifact row falls back to the
    legacy ``ReleaseVersion`` scalar projection. The scalar is projected AS-IS:
    a promotion that was never armed for serving confirmation (no row, scalar
    NULL) reads as plain ``promoted`` (NA), never a synthesized ``unconfirmed``.
    Inventing ``unconfirmed`` for an un-armed promotion creates a pending state
    the sweep can never resolve, wedging the next version. When no artifact
    evidence exists at all (for example, an open version that has never
    promoted), the helper preserves the scalar truth instead of inventing one.

    When ``project`` is supplied the backend_revision legacy-scalar projection is
    gated on the backend_revision provider being OBSERVABLE for that project (the
    deploy-level identity matches): for an external project the Cloud Run backend
    revision is unobservable and has no row, so projecting the version scalar
    onto it would invent a misleading ``backend_revision`` signal (and a phantom
    "waiting on backend_revision" gap during the merge-level confirmation
    window). A missing row for a non-observable provider stays vacuous (None).
    With ``project=None`` (legacy callers) the projection is unconditional, so
    existing self-repo behavior is byte-for-byte preserved.
    """

    if project is not None and identity is None:
        identity = deploy_repo_identity()
    observable_kinds: set[str] | None = (
        None
        if project is None
        else {
            provider.artifact_kind
            for provider in observable_release_artifact_providers(project, identity)
        }
    )

    artifact_by_kind = {
        artifact.artifact_kind: artifact
        for artifact in (artifacts or [])
        if artifact.artifact_kind
    }
    evidence_rows: list[ReleaseArtifactEvidence] = []
    aggregate_rows: list[tuple[ReleaseArtifactProvider, ReleaseArtifactEvidence]] = []

    for provider in iter_release_artifact_providers():
        artifact = artifact_by_kind.get(provider.artifact_kind)
        backend_scalar_projectable = is_backend_revision_artifact(
            provider.artifact_kind
        ) and (observable_kinds is None or provider.artifact_kind in observable_kinds)
        if artifact is not None:
            evidence = ReleaseArtifactEvidence(
                artifact_kind=provider.artifact_kind,
                required=provider.required,
                serving_status=artifact.serving_status,
                served_identifier=artifact.served_identifier,
                served_at=artifact.served_at,
                reason_code=artifact.reason_code,
            )
        elif backend_scalar_projectable:
            # No backend artifact row — project the legacy ReleaseVersion scalar
            # DIRECTLY. Do NOT manufacture "unconfirmed" from status=="promoted":
            # serving-confirmation arming (serving_status="unconfirmed" + the
            # backend artifact row) only happens for promotions whose deploy is
            # observable; a promotion that was never armed has serving_status
            # NULL and NO rows. Synthesizing "unconfirmed" for it invents a
            # pending serving state that NOTHING can ever resolve — the sweep
            # only selects versions with serving_status=="unconfirmed" OR a real
            # pending artifact row, so an un-armed promotion would sit falsely
            # "unconfirmed" forever and wedge the next version (the real v3..v7
            # case). This mirrors the non-backend branch below, which already
            # treats a missing row as vacuous rather than a false negative. An
            # armed promotion always persists its scalar AND its row in the same
            # transaction, so the legitimate pending state is reported either by
            # the real artifact row (above) or the scalar here.
            evidence = ReleaseArtifactEvidence(
                artifact_kind=provider.artifact_kind,
                required=provider.required,
                serving_status=version.serving_status,
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
        # A required row with serving_status None is VACUOUS, not pending — it
        # mirrors the aggregate projection (``summarize_release_artifacts``
        # excludes None-status evidence from the aggregate; #8964) and the
        # un-armed/legacy promotion case (no row, scalar NULL → plain
        # "promoted"/NA).  Counting it as PENDING was the v8 incident: an
        # un-armed promotion whose required backend_revision/agent_image rows
        # were never created (or whose legacy scalar is NULL) projected a
        # phantom "waiting on required artifacts: backend_revision, agent_image"
        # for a version that verifiably shipped.  Skip it so only rows with a
        # real (non-None) status drive the gap.
        if status is None:
            continue
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


def _effective_confirm_deadline(
    confirmation_deadline: datetime | None,
    deploy_confirm_deadline: datetime | None,
    promoted_at: datetime | None,
) -> datetime | None:
    """Resolve the effective serving-confirmation deadline for a provider check.

    Order of precedence — mirrors ``_classify_serving_outcome``'s backstop and
    the (now unified) inline copies this helper replaces:

      * ``artifact.confirmation_deadline`` wins when stamped;
      * else ``version.deploy_confirm_deadline`` (the legacy scalar, kept
        mirrored to artifact rows by the backend-revision projection);
      * else ``promoted_at + _DEPLOY_CONFIRM_WINDOW_SECONDS`` — the backstop
        that keeps a promotion recorded WITHOUT a stamped deadline from
        deferring forever.

    Returns ``None`` only when there is no ``promoted_at`` to derive a window
    from.  The result is naive UTC (``_to_naive_utc``) so callers can compare it
    directly against their ``now``.
    """
    from app.release_reconciliation import _to_naive_utc

    deadline = confirmation_deadline or deploy_confirm_deadline
    if deadline is None and promoted_at is not None:
        from app.release_promotion import _DEPLOY_CONFIRM_WINDOW_SECONDS

        deadline = promoted_at + timedelta(seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS)
    return _to_naive_utc(deadline)


async def _reclassify_sha_mismatch_if_superseded(
    session: AsyncSession,
    version: ReleaseVersion,
    ds: DeploymentState | None,
    decision: ServingDecision | None,
    now: datetime,
) -> ServingDecision | None:
    """Treat supersession-by-descendant as serving-confirmed, not sha_mismatch.

    ``_classify_serving_outcome`` does an exact ``current_sha == promoted_sha``
    match, so a promotion whose target branch legitimately advanced PAST the
    promoted commit before the sweep first observed the live SHA classifies as
    ``deploy_failed``/``sha_mismatch`` — a false deploy failure (the real v9
    incident: the promoted merge SHA verifiably served, then a DESCENDANT commit
    containing it deployed before the confirmation sweep first checked).

    On (and only on) the rare ``sha_mismatch`` path, ask GitHub — via the compare
    API (base = promoted SHA, head = observed live SHA) through the same
    credential pattern as the ``promotion_merge`` provider — whether the promoted
    commit is an ancestor of what is serving:

      * descendant (head strictly contains the promoted commit) → the deploy
        SUCCEEDED and was superseded: resolve ``serving_confirmed`` with the
        OBSERVED live SHA as the served identifier (never the promoted SHA) and
        the distinct ``superseded_by_descendant`` reason code;
      * diverged / behind / unrelated that booted AFTER the promotion was
        stamped (``deployed_at > promoted_at``) → keep
        ``deploy_failed``/``sha_mismatch`` exactly as today — a revision that
        arrived after the promotion but does NOT contain the promoted commit is
        a genuinely wrong deploy;
      * diverged / behind / unrelated that PREDATES the promotion
        (``deployed_at <= promoted_at``) → the OLD revision is still serving
        while the deploy is in flight (or never arrived).  That is not an
        active failure: keep the conservative pre-fix semantics — wait within
        the confirmation window and time out honestly past the deadline, so a
        genuinely absent revision never hard-fails and never silently confirms.
        The classifier now routes every STABLE mismatch through this check, so
        this branch is what preserves the timeout path for absent revisions;
      * any credential/API failure → fall back to the current behavior
        (``deploy_failed``/``sha_mismatch``) with a warning — fail toward
        existing semantics, never silently confirm.

    The classifier itself stays PURE (no I/O); this consumer-layer hook owns the
    one extra GitHub call.  Lazy imports avoid a load-time cycle
    (``release_reconciliation`` imports this module).
    """
    if (
        decision is None
        or decision[0] != "deploy_failed"
        or decision[1] != "sha_mismatch"
    ):
        return decision

    from app import github
    from app.release_promotion import _resolve_github_with_session
    from app.release_reconciliation import _to_naive_utc

    promoted_sha = version.promoted_sha
    observed_sha = (ds.current_sha or None) if ds is not None else None
    if not promoted_sha or not observed_sha:
        return decision
    project = await session.get(Project, version.project_id)
    if project is None:
        return decision

    try:
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            logger.warning(
                "[RELEASE_ARTIFACTS] sha_mismatch supersession check for v%s: "
                "no GitHub credentials — keeping deploy_failed/sha_mismatch",
                getattr(version, "version_number", "?"),
            )
            return decision
        token, owner, repo = resolved
        ancestry = await github.is_ancestor_of(
            token,
            owner,
            repo,
            promoted_sha,
            observed_sha,
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — fall back to existing semantics
        logger.warning(
            "[RELEASE_ARTIFACTS] sha_mismatch supersession check for v%s raised "
            "(%s) — keeping deploy_failed/sha_mismatch",
            getattr(version, "version_number", "?"),
            exc,
        )
        return decision
    if ancestry is not True:
        if ancestry is None:
            # Ancestry indeterminate (GitHub compare API unavailable).  Within
            # the confirmation window, defer so the next sweep can retry once
            # the API recovers.  Past the deadline, resolve as a soft
            # confirmation_timed_out rather than a hard deploy_failed: we
            # couldn't verify descendant vs. diverged but never observed an
            # active failure — mirrors the _defer_or_timeout() pattern in the
            # promotion_merge provider.
            _deadline = _effective_confirm_deadline(
                None, version.deploy_confirm_deadline, version.promoted_at
            )
            _now_naive = _to_naive_utc(now) or now
            if _deadline is None or _now_naive < _deadline:
                return None  # within window — defer, retry on next sweep
            logger.warning(
                "[RELEASE_ARTIFACTS] sha_mismatch supersession check for v%s: "
                "ancestry indeterminate (compare API failed) past deadline — "
                "resolving confirmation_timed_out",
                getattr(version, "version_number", "?"),
            )
            return (
                "confirmation_timed_out",
                "backend_revision_ancestry_indeterminate",
                None,
            )
        # ancestry is determinately False — the observed revision does NOT
        # contain the promoted commit.  Whether that is a hard failure or
        # "still waiting" depends on the deploy/promote ordering (see the
        # docstring): a revision that PREDATES the promotion is the old one
        # still serving while the deploy is in flight (or never arrived) —
        # keep waiting within the window and time out honestly past the
        # deadline, never hard-failing and never silently confirming.  A
        # revision that booted AFTER the promotion yet lacks the promoted
        # commit is a genuinely wrong deploy — keep deploy_failed/sha_mismatch.
        _deployed_at = (
            _to_naive_utc(getattr(ds, "deployed_at", None))
            if ds is not None
            else None
        )
        _promoted_at_naive = _to_naive_utc(version.promoted_at)
        if not (
            _deployed_at is not None
            and _promoted_at_naive is not None
            and _deployed_at > _promoted_at_naive
        ):
            _deadline = _effective_confirm_deadline(
                None, version.deploy_confirm_deadline, version.promoted_at
            )
            _now_naive = _to_naive_utc(now) or now
            if _deadline is None or _now_naive < _deadline:
                return None  # within window — defer, retry on next sweep
            logger.warning(
                "[RELEASE_ARTIFACTS] sha_mismatch supersession check for v%s: "
                "observed revision predates the promotion and is not a "
                "descendant — no descendant revision observed by the deadline; "
                "resolving confirmation_timed_out",
                getattr(version, "version_number", "?"),
            )
            # Substantiate the timeout: consult the deploy workflow runs for the
            # promoted SHA so the operator sees whether the deploy was never
            # dispatched, ran and failed, or ran and left a stale revision —
            # instead of the low-information no_new_revision backstop.
            return await _substantiate_no_new_revision(session, version, now)
        return decision

    served_at = _to_naive_utc(ds.deployed_at) if ds is not None else None
    logger.info(
        "[RELEASE_ARTIFACTS] promoted v%s SHA %s is an ancestor of the observed "
        "live SHA %s — the deploy succeeded and was superseded by a descendant; "
        "resolving serving_confirmed (%s), not deploy_failed/sha_mismatch",
        getattr(version, "version_number", "?"),
        (promoted_sha or "")[:12],
        (observed_sha or "")[:12],
        SUPERSEDED_BY_DESCENDANT_REASON,
    )
    return (
        "serving_confirmed",
        observed_sha,
        served_at,
        SUPERSEDED_BY_DESCENDANT_REASON,
    )


async def _substantiate_no_new_revision(
    session: AsyncSession,
    version: ReleaseVersion,
    now: datetime,
) -> ServingDecision:
    """Name the deploy-workflow gate behind a bare ``no_new_revision`` timeout.

    ``no_new_revision`` ("the serving SHA just couldn't be confirmed in time")
    cannot distinguish the real causes of a promotion whose deploy never
    shipped: the deploy workflow was never dispatched, it ran and failed, or it
    ran and the revision is stale.  The run-listing helper
    (:func:`app.github.get_workflow_run_for_sha`) already exists and is used by
    the agent-image bake gate; here it is pointed at the deploy workflow for the
    promoted SHA to substantiate the cause:

      * no deploy.yml run for the promoted SHA → ``deploy_workflow_never_dispatched``
      * a run concluded non-success → ``deploy_workflow_failed``
      * a run concluded success → ``deploy_workflow_succeeded_stale_revision``
      * a run still queued/in_progress past the deadline → ``deploy_workflow_in_progress``

    Any credential/API failure keeps the honest ``no_new_revision`` backstop —
    never fabricate a specific gate from a failed lookup.  The outcome stays a
    ``confirmation_timed_out`` (a report, never a silent confirm), matching the
    conservative wait → honest-timeout contract of the supersession reclassifier.
    """
    promoted_sha = version.promoted_sha
    if not promoted_sha:
        return ("confirmation_timed_out", "no_new_revision", None)
    project = await session.get(Project, version.project_id)
    if project is None:
        return ("confirmation_timed_out", "no_new_revision", None)
    try:
        from app import github
        from app.github_checks import DEPLOY_WORKFLOW_FILE
        from app.release_promotion import _resolve_github_with_session

        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return ("confirmation_timed_out", "no_new_revision", None)
        token, owner, repo = resolved
        run = await github.get_workflow_run_for_sha(
            token,
            owner,
            repo,
            promoted_sha,
            workflow_file=DEPLOY_WORKFLOW_FILE,
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — fall back to the honest backstop
        logger.warning(
            "[RELEASE_ARTIFACTS] no_new_revision substantiation for v%s raised "
            "(%s) — keeping no_new_revision",
            getattr(version, "version_number", "?"),
            exc,
        )
        return ("confirmation_timed_out", "no_new_revision", None)
    if run is None:
        # No deploy.yml run exists for the promoted SHA — the deploy workflow
        # was NEVER dispatched.  (A run-listing API failure also surfaces as
        # None; the outcome stays confirmation_timed_out — a report, never a
        # silent confirm, and the upstream helper logs the API failure.)
        return (
            "confirmation_timed_out",
            DEPLOY_WORKFLOW_NEVER_DISPATCHED_REASON,
            None,
        )
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status == "completed":
        if conclusion == "success":
            return (
                "confirmation_timed_out",
                DEPLOY_WORKFLOW_SUCCEEDED_STALE_REVISION_REASON,
                None,
            )
        return ("confirmation_timed_out", DEPLOY_WORKFLOW_FAILED_REASON, None)
    return ("confirmation_timed_out", DEPLOY_WORKFLOW_IN_PROGRESS_REASON, None)


async def _check_backend_revision_serving(
    session: AsyncSession,
    version: ReleaseVersion,
    artifact: ReleaseDeployArtifact,
    now: datetime,
) -> ServingDecision | None:
    """Adapt the existing deploy-health classifier into the provider registry."""

    from app.release_reconciliation import _classify_serving_outcome

    ds = await session.get(DeploymentState, 1)
    decision = _classify_serving_outcome(
        ds,
        version.promoted_at,
        version.promoted_sha,
        artifact.confirmation_deadline or version.deploy_confirm_deadline,
        now,
    )
    # A sha_mismatch may be SUPERSESSION (main advanced past the promoted commit
    # before confirmation resolved) rather than a failed deploy — distinguish via
    # one GitHub ancestry call on this rare path only.  Both the initial
    # confirmation sweep and the deploy-failed recovery re-check consume the
    # classifier through this provider, so a falsely-latched version self-heals
    # within its recovery window.
    return await _reclassify_sha_mismatch_if_superseded(
        session, version, ds, decision, now
    )


register_release_artifact_provider(
    BACKEND_REVISION_ARTIFACT_KIND,
    _check_backend_revision_serving,
    required=True,
    # Cloud Run backend revision (DEPLOY_SHA resolver / DeploymentState) is only
    # observable for the project this deployment was built from.
    observable=_observable_for_deploy_identity,
)


def _previously_deployed_sha(
    ds: DeploymentState | None,
    promoted_sha: str,
    *,
    current_contains_promoted: bool = False,
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
    if current and (current == promoted_sha or current_contains_promoted):
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
        yet) — treat as NOT-YET-CONFIRMED (return ``None``) WITHIN the
        confirmation window, never ``deploy_failed``.  Past the
        ``confirmation_deadline`` the indeterminate paths time out to
        ``confirmation_timed_out`` (exactly like the promotion_merge and
        backend_revision providers) so a bake-path promotion whose fleet never
        serves the promoted SHA resolves instead of pinning the aggregate
        ``unconfirmed`` forever.  The version then resolves within the existing
        confirmation/recovery windows, and the recovery sweep can re-check the
        timed-out artifact when the fleet later catches up.

    Lazy imports avoid a load-time cycle (``release_promotion`` imports this
    module), mirroring ``_check_backend_revision_serving``.
    """
    from app import compute, github_refs
    from app.models import Project
    from app.release_promotion import _resolve_github_with_session
    from app.release_reconciliation import _to_naive_utc

    promoted_sha = version.promoted_sha
    if not promoted_sha:
        return None

    # Effective confirmation deadline — shared backstop (artifact → version →
    # promoted_at + window) so every indeterminate path is bounded, never waited
    # on forever.  Mirrors _classify_serving_outcome and the promotion_merge
    # provider, now unified in one resolver.
    _deadline = _effective_confirm_deadline(
        artifact.confirmation_deadline,
        version.deploy_confirm_deadline,
        version.promoted_at,
    )
    _now_naive = _to_naive_utc(now) or now

    def _defer_or_timeout(reason: str) -> ServingDecision | None:
        """Return confirmation_timed_out if the deadline elapsed, else None."""
        if _deadline is not None and _now_naive >= _deadline:
            return ("confirmation_timed_out", reason, None)
        return None

    project = await session.get(Project, version.project_id)
    if not project_matches_deploy_identity(project):
        # The agent VM image gate is scoped to the deploy's OWN repo — only the
        # deployment that runs the agent fleet can observe this dimension.  The
        # provider's observability predicate already withholds the artifact from
        # any project that is not the deploy identity, so an armed agent_image
        # row only exists for the deploy project; this is reached only if a
        # generic refresh lazily created one for a mismatched project.  There is
        # no agent fleet to gate on there, so the dimension is VACUOUSLY served:
        # confirm immediately rather than strand the artifact unconfirmed (the
        # same "never strand a bake that will never run" principle as the
        # no-bake-path branch below).  Deliberately does NOT read
        # ``project.is_self_repo`` (deprecated for deploy-level observability).
        return ("serving_confirmed", promoted_sha, now)

    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        # No GitHub credentials → indeterminate — times out at the deadline
        # rather than waiting forever (mirrors _check_promotion_merge_serving).
        return _defer_or_timeout("agent_image_no_github_credentials")
    token, owner, repo = resolved

    ds = await session.get(DeploymentState, 1)
    current_sha = (getattr(ds, "current_sha", None) or "") if ds is not None else ""
    current_contains_promoted = False
    if (
        current_sha
        and current_sha != promoted_sha
        and getattr(ds, "prior_deploy_sha", None)
    ):
        try:
            ancestry = await github_refs.is_ancestor_of(
                token,
                owner,
                repo,
                promoted_sha,
                current_sha,
                project_id=str(version.project_id),
            )
        except Exception:  # noqa: BLE001 - ancestry lookup is an indeterminate serving signal
            ancestry = None
        if ancestry is None:
            return _defer_or_timeout("agent_image_ancestry_indeterminate")
        current_contains_promoted = ancestry is True

    base_sha = _previously_deployed_sha(
        ds,
        promoted_sha,
        current_contains_promoted=current_contains_promoted,
    )
    if not base_sha:
        # Diff base unobservable → indeterminate → not-yet-confirmed within the
        # window; times out past the deadline.
        logger.info(
            "agent_image_serving_indeterminate path=base_sha_unobservable"
            " version_id=%s promoted_sha=%s",
            version.id,
            promoted_sha,
        )
        return _defer_or_timeout("agent_image_base_sha_unobservable")

    rebake = await github_refs.agent_image_rebake_expected(
        token,
        owner,
        repo,
        base_sha,
        promoted_sha,
        project_id=str(version.project_id),
    )
    if rebake is None:
        # API/ref error → indeterminate → not-yet-confirmed within the window;
        # times out past the deadline.
        logger.info(
            "agent_image_serving_indeterminate path=rebake_lookup_indeterminate"
            " version_id=%s promoted_sha=%s",
            version.id,
            promoted_sha,
        )
        return _defer_or_timeout("agent_image_rebake_lookup_indeterminate")
    if rebake is False:
        # No bake-path change → no agent rebake required → confirm immediately so
        # the aggregate tracks the backend revision alone.
        return ("serving_confirmed", promoted_sha, now)

    # Bake expected: confirm only once the fleet is independently observed
    # serving the promoted SHA (the freshly baked image is live and running).
    if await compute.fleet_serving_source_sha(
        session,
        promoted_sha,
        allow_descendant=True,
        token=token,
        owner=owner,
        repo=repo,
        project_id=str(version.project_id),
    ):
        return ("serving_confirmed", promoted_sha, now)
    logger.info(
        "agent_image_serving_indeterminate path=fleet_not_yet_serving"
        " version_id=%s promoted_sha=%s",
        version.id,
        promoted_sha,
    )
    return _defer_or_timeout("agent_image_fleet_not_yet_serving")


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
        # The agent VM image / fleet serving signal is observable only for the
        # deploy's own repo (the deployment that runs the agent fleet).
        observable=_observable_for_deploy_identity,
    )


async def _check_promotion_merge_serving(
    session: AsyncSession,
    version: ReleaseVersion,
    artifact: ReleaseDeployArtifact,
    now: datetime,
) -> ServingDecision | None:
    """Baseline merge-level serving check — observable for EVERY GitHub project.

    Confirms the promotion actually persisted by asking GitHub (through the
    project's own credentials) two things:

      1. Is ``promoted_sha`` STILL reachable from the promotion target branch
         head?  If a merge was later reverted / force-pushed away, the promoted
         commit is no longer an ancestor of the target head — a real failure
         (``deploy_failed`` / ``merge_reverted``).
      2. When a deploy/release workflow ran on that SHA, did it conclude
         successfully?  A completed-but-failed required check on the merge
         commit downgrades to ``deploy_failed`` (``deploy_workflow_failed``);
         an in-flight check defers (``None`` → keep waiting).

    Resolution:
      * ancestry indeterminate (API error) → ``None`` (the bounded confirmation
        window eventually times this out — never a false failure);
      * ancestry False (merge not persisted) → ``deploy_failed`` /
        ``merge_reverted``;
      * ancestry True + checks success / no checks → ``serving_confirmed`` so
        every promotion, for any project, gets at least a merge-level verdict;
      * ancestry True + checks completed-failure → ``deploy_failed`` /
        ``deploy_workflow_failed``;
      * ancestry True + checks in-flight → ``None`` (defer).

    Lazy imports avoid a load-time cycle (``release_promotion`` imports this
    module), mirroring ``_check_backend_revision_serving``.
    """
    from app import github
    from app.release_promotion import (
        _lts_branch_names,
        _resolve_github_with_session,
    )
    from app.release_reconciliation import _to_naive_utc

    promoted_sha = version.promoted_sha
    if not promoted_sha:
        # _confirm_serving_or_fail handles this with its promoted_sha_missing bypass.
        return None

    project = await session.get(Project, version.project_id)
    if project is None:
        return None

    # Effective confirmation deadline — shared backstop (artifact → version →
    # promoted_at + window), unified with the backend_revision / agent_image
    # providers via _effective_confirm_deadline.  Used below to time out
    # bounded-defer (indeterminate) paths instead of waiting forever.
    _deadline = _effective_confirm_deadline(
        artifact.confirmation_deadline,
        version.deploy_confirm_deadline,
        version.promoted_at,
    )
    _now_naive = _to_naive_utc(now) or now

    def _defer_or_timeout() -> ServingDecision | None:
        """Return confirmation_timed_out if deadline elapsed, else None (keep waiting)."""
        if _deadline is not None and _now_naive >= _deadline:
            return ("confirmation_timed_out", "promotion_merge_indeterminate", None)
        return None

    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        # No credentials → indeterminate — times out at deadline rather than waiting forever.
        return _defer_or_timeout()
    token, owner, repo = resolved
    _integration_branch, promotion_target = _lts_branch_names(project)

    try:
        ancestry = await github.is_ancestor_of(
            token,
            owner,
            repo,
            promoted_sha,
            promotion_target,
            project_id=str(version.project_id),
        )
    except Exception:  # noqa: BLE001 — a check failure must not crash the sweep
        return _defer_or_timeout()
    if ancestry is None:
        # Indeterminate (API error / ref missing) — times out at deadline.
        return _defer_or_timeout()
    if ancestry is False:
        # The promoted SHA is no longer reachable from the promotion target —
        # the merge was reverted / rewritten away.  A real, loud failure.
        return ("deploy_failed", "merge_reverted", None)

    # Merge persisted.  Consult the deploy/release workflow conclusion on the
    # merge commit as a secondary signal — best-effort: any lookup failure
    # degrades to the merge-level confirmation rather than a false failure.
    try:
        from app import github_checks

        checks = await github_checks.get_check_runs_cached(
            token,
            owner,
            repo,
            promoted_sha,
            project_id=str(version.project_id),
            treat_missing_as_success=True,
        )
        status = checks.get("status")
        conclusion = checks.get("conclusion")
        if status in ("in_progress", "pending"):
            # A deploy/release workflow is still running — defer to deadline.
            return _defer_or_timeout()
        if status == "completed" and conclusion not in ("success", None):
            return ("deploy_failed", "deploy_workflow_failed", None)
    except Exception as exc:  # noqa: BLE001 — workflow lookup is a best-effort secondary signal
        # A check-run lookup failure must NOT manufacture a deploy failure — the
        # merge demonstrably persisted (ancestry True above), so degrade to the
        # merge-level confirmation rather than a false negative.
        logger.debug(
            "[RELEASE_ARTIFACTS] promotion_merge workflow lookup failed for v%s "
            "(non-fatal; confirming merge-level): %s",
            getattr(version, "version_number", "?"),
            exc,
        )

    return ("serving_confirmed", promoted_sha, now)


# Baseline merge-level serving provider.  Observable for EVERY GitHub project
# (its signal is the promotion target branch, reachable via the project's own
# credentials), so every promotion — for any project, not just the deploy's own
# repo — gets armed and resolves to at least a merge-persisted verdict.
if PROMOTION_MERGE_ARTIFACT_KIND not in _ARTIFACT_PROVIDERS:
    register_release_artifact_provider(
        PROMOTION_MERGE_ARTIFACT_KIND,
        _check_promotion_merge_serving,
        required=True,
        observable=_observable_for_any_github_project,
    )


def version_serving_summary(
    version: ReleaseVersion,
    project: Project | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Canonical single-call serving resolver for read paths.

    Wraps ``summarize_release_artifacts`` with the standard ``deploy_artifacts``
    eager-load accessor so callers never inline the accessor or duplicate the
    call.  All read paths (system state, version lifecycle, strategist) must go
    through this wrapper rather than calling ``summarize_release_artifacts``
    directly or maintaining their own private copies.
    """
    return summarize_release_artifacts(
        version,
        getattr(version, "deploy_artifacts", None),
        project=project,
    )
