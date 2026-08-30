"""Exact, replay-safe operator release-version cuts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Project, ReleaseVersion
from app.release_authority_lock import lock_project_then_version
from app.time_utils import _coerce_naive_utc, _utcnow, utc_iso


logger = logging.getLogger(__name__)

_MODULE_SIZE_GOVERNANCE_CAP: int = 1400
_CUT_RECEIPT_SCHEMA_VERSION = 1
_CUT_FOLLOWUP_SCHEMA_VERSION = 1
_CUT_REASON_MAX = 500
_CUT_IDEMPOTENCY_KEY_MAX = 255
_CUT_FOLLOWUP_LEASE_SECONDS = 300
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class VersionCutIdentityUnavailable(RuntimeError):
    """The live integration identity required for a cut is unavailable."""


class VersionCutPreconditionFailed(RuntimeError):
    """The exact version or integration-head precondition no longer holds."""

    def __init__(self, message: str, *, current: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.current = dict(current or {})


class VersionCutIdempotencyConflict(RuntimeError):
    """One idempotency key was already bound to a different or corrupt cut."""


@dataclass(frozen=True)
class VersionCutIntegrationIdentity:
    owner: str
    repo: str
    branch: str
    head_sha: str
    observed_at: datetime

    def identity_token(self) -> str:
        payload = {
            "schema_version": _CUT_RECEIPT_SCHEMA_VERSION,
            "owner": self.owner,
            "repo": self.repo,
            "branch": self.branch,
            "head_sha": self.head_sha,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def projection(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "repo": self.repo,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "identity_token": self.identity_token(),
            "observed_at": utc_iso(self.observed_at),
        }


@dataclass(frozen=True)
class VersionCutOutcome:
    abandoned: ReleaseVersion
    successor: ReleaseVersion
    receipt: dict[str, Any]
    replayed: bool

    def __iter__(self) -> Iterator[ReleaseVersion]:
        # Preserve the established two-value destructuring contract while the
        # router consumes the additional exact receipt/replay fields.
        yield self.abandoned
        yield self.successor


@dataclass(frozen=True)
class VersionCutFollowupResult:
    complete: bool
    executed: bool


def _normalize_idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("idempotency_key must be non-empty")
    if len(key) > _CUT_IDEMPOTENCY_KEY_MAX:
        raise ValueError(
            f"idempotency_key must be at most {_CUT_IDEMPOTENCY_KEY_MAX} characters"
        )
    return key


def _normalize_expected_sha(value: str) -> str:
    sha = str(value or "").strip().lower()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise ValueError("expected_integration_sha must be a full 40-character SHA")
    return sha


def _normalized_reason(value: str | None) -> str | None:
    return value[:_CUT_REASON_MAX] if value is not None else None


def _cut_request_fingerprint(
    *,
    project_id: uuid.UUID,
    operator_id: str,
    reason: str | None,
    expected_version_id: uuid.UUID,
    expected_version_number: int,
    expected_integration_sha: str,
    expected_integration_identity: str,
) -> str:
    identity_token = str(expected_integration_identity or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", identity_token):
        raise ValueError("expected_integration_identity must be a 64-character hash")
    payload = {
        "schema_version": _CUT_RECEIPT_SCHEMA_VERSION,
        "project_id": str(project_id),
        "operator_id": str(operator_id),
        "reason": _normalized_reason(reason),
        "expected_version_id": str(expected_version_id),
        "expected_version_number": int(expected_version_number),
        "expected_integration_sha": _normalize_expected_sha(
            expected_integration_sha
        ),
        "expected_integration_identity": identity_token,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cut_receipt_from_version(version: ReleaseVersion) -> dict[str, Any] | None:
    qualification = version.last_qualification
    if not isinstance(qualification, dict):
        return None
    receipt = qualification.get("version_cut_receipt")
    return dict(receipt) if isinstance(receipt, dict) else None


def _canonical_utc_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise VersionCutIdempotencyConflict(
            f"version-cut {field} is not a timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise VersionCutIdempotencyConflict(
            f"version-cut {field} is not a timestamp"
        ) from exc
    if parsed.tzinfo is None or utc_iso(parsed) != value:
        raise VersionCutIdempotencyConflict(
            f"version-cut {field} is not canonical UTC"
        )
    return parsed


def version_cut_followup_projection(
    version: ReleaseVersion,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the mutable, canonical delivery state beside an immutable receipt."""

    qualification = version.last_qualification
    if not isinstance(qualification, dict):
        return None
    bound_receipt = receipt or _cut_receipt_from_version(version)
    marker = qualification.get("version_cut_followups")
    if marker is None:
        if isinstance(bound_receipt, dict):
            raise VersionCutIdempotencyConflict(
                "exact version-cut receipt is missing its follow-up marker"
            )
        return None
    if not isinstance(marker, dict):
        raise VersionCutIdempotencyConflict(
            "version-cut follow-up marker is not an object"
        )
    fingerprint = (
        bound_receipt.get("request_fingerprint")
        if isinstance(bound_receipt, dict)
        else None
    )
    attempts = marker.get("attempts")
    status = marker.get("status")
    if (
        marker.get("schema_version") != _CUT_FOLLOWUP_SCHEMA_VERSION
        or not isinstance(fingerprint, str)
        or marker.get("request_fingerprint") != fingerprint
        or status not in {"pending", "complete"}
        or type(attempts) is not int
        or attempts < 0
    ):
        raise VersionCutIdempotencyConflict(
            "version-cut follow-up marker does not match its receipt"
        )
    _canonical_utc_timestamp(marker.get("created_at"), "follow-up created_at")
    last_attempt_at = marker.get("last_attempt_at")
    if attempts == 0:
        if last_attempt_at is not None:
            raise VersionCutIdempotencyConflict(
                "version-cut follow-up attempt timestamp has no attempt"
            )
    else:
        _canonical_utc_timestamp(
            last_attempt_at,
            "follow-up last_attempt_at",
        )
    completed_at = marker.get("completed_at")
    last_error = marker.get("last_error")
    claim_token = marker.get("claim_token")
    claim_expires_at = marker.get("claim_expires_at")
    if status == "complete":
        _canonical_utc_timestamp(completed_at, "follow-up completed_at")
        if last_error is not None or claim_token is not None or claim_expires_at is not None:
            raise VersionCutIdempotencyConflict(
                "completed version-cut follow-up marker retains mutable claim state"
            )
    else:
        if completed_at is not None:
            raise VersionCutIdempotencyConflict(
                "pending version-cut follow-up marker has completed_at"
            )
        if last_error is not None and (
            not isinstance(last_error, str) or len(last_error) > 500
        ):
            raise VersionCutIdempotencyConflict(
                "version-cut follow-up marker has invalid last_error"
            )
        if (claim_token is None) != (claim_expires_at is None):
            raise VersionCutIdempotencyConflict(
                "version-cut follow-up marker has an incomplete claim"
            )
        if claim_token is not None:
            try:
                uuid.UUID(str(claim_token))
            except (TypeError, ValueError) as exc:
                raise VersionCutIdempotencyConflict(
                    "version-cut follow-up marker has an invalid claim token"
                ) from exc
            _canonical_utc_timestamp(
                claim_expires_at,
                "follow-up claim_expires_at",
            )
    return {
        "schema_version": marker["schema_version"],
        "status": marker["status"],
        "attempts": attempts,
        "created_at": marker.get("created_at"),
        "last_attempt_at": marker.get("last_attempt_at"),
        "completed_at": marker.get("completed_at"),
        "last_error": marker.get("last_error"),
        "lease_expires_at": marker.get("claim_expires_at"),
    }


def _validate_cut_receipt(
    receipt: dict[str, Any],
    *,
    project_id: uuid.UUID,
    idempotency_key: str,
    prior: ReleaseVersion,
    request_fingerprint: str | None,
) -> None:
    required = {
        "schema_version": _CUT_RECEIPT_SCHEMA_VERSION,
        "status": "committed",
        "project_id": str(project_id),
        "idempotency_key": idempotency_key,
        "abandoned_version_id": str(prior.id),
        "abandoned_version_number": prior.version_number,
        "abandoned_status": "abandoned",
    }
    for field, expected in required.items():
        if field not in receipt or receipt[field] != expected:
            raise VersionCutIdempotencyConflict(
                f"stored version-cut receipt is invalid: {field}"
            )
    fingerprint = receipt.get("request_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has no request fingerprint"
        )
    if request_fingerprint is not None and fingerprint != request_fingerprint:
        raise VersionCutIdempotencyConflict(
            "idempotency key is already bound to a different version-cut request"
        )
    expected_version_number = receipt.get("expected_version_number")
    expected_version_id = receipt.get("expected_version_id")
    expected_sha = receipt.get("expected_integration_sha")
    expected_identity = receipt.get("expected_integration_identity")
    operator_id = receipt.get("operator_id")
    operator_display = receipt.get("operator_display")
    reason = receipt.get("reason")
    if (
        type(expected_version_number) is not int
        or expected_version_number != prior.version_number
        or expected_version_id != str(prior.id)
        or not isinstance(expected_sha, str)
        or not isinstance(expected_identity, str)
        or not isinstance(operator_id, str)
        or not isinstance(operator_display, str)
        or not operator_display
        or (reason is not None and not isinstance(reason, str))
    ):
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has invalid request identity"
        )
    if reason != _normalized_reason(reason):
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has a non-canonical reason"
        )
    try:
        recomputed = _cut_request_fingerprint(
            project_id=project_id,
            operator_id=operator_id,
            reason=reason,
            expected_version_id=prior.id,
            expected_version_number=expected_version_number,
            expected_integration_sha=expected_sha,
            expected_integration_identity=expected_identity,
        )
    except (TypeError, ValueError) as exc:
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has invalid request identity"
        ) from exc
    if recomputed != fingerprint:
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt request fingerprint is inconsistent"
        )
    observed_owner = receipt.get("observed_repository_owner")
    observed_repo = receipt.get("observed_repository_name")
    observed_branch = receipt.get("observed_integration_branch")
    observed_sha = receipt.get("observed_integration_sha")
    if not all(
        isinstance(value, str) and value
        for value in (observed_owner, observed_repo, observed_branch, observed_sha)
    ):
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has incomplete observed identity"
        )
    try:
        observed_at = datetime.fromisoformat(str(receipt.get("observed_at")))
        committed_at = datetime.fromisoformat(str(receipt.get("committed_at")))
    except (TypeError, ValueError) as exc:
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has invalid timestamp"
        ) from exc
    try:
        observed_identity = VersionCutIntegrationIdentity(
            owner=observed_owner,
            repo=observed_repo,
            branch=observed_branch,
            head_sha=_normalize_expected_sha(observed_sha),
            observed_at=observed_at,
        )
        normalized_expected_sha = _normalize_expected_sha(expected_sha)
    except (TypeError, ValueError) as exc:
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has invalid integration identity"
        ) from exc
    if (
        utc_iso(observed_at) != receipt.get("observed_at")
        or utc_iso(committed_at) != receipt.get("committed_at")
        or observed_identity.identity_token() != expected_identity
        or observed_identity.head_sha != normalized_expected_sha
        or receipt.get("successor_base_sha") != expected_sha
        or receipt.get("abandoned_base_sha") != prior.base_sha
        or receipt.get("abandoned_candidate_sha") != prior.candidate_sha
        or receipt.get("abandoned_opened_at") != utc_iso(prior.opened_at)
    ):
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt does not match its frozen version identity"
        )
    successor_id = receipt.get("successor_version_id")
    successor_number = receipt.get("successor_version_number")
    try:
        successor_opened_at = datetime.fromisoformat(
            str(receipt.get("successor_opened_at"))
        )
    except (TypeError, ValueError) as exc:
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has invalid successor timestamp"
        ) from exc
    if (
        not isinstance(successor_id, str)
        or type(successor_number) is not int
        or successor_number != prior.version_number + 1
        or receipt.get("successor_status") != "open"
        or receipt.get("successor_phase") != "build"
        or receipt.get("successor_goal") is not None
        or receipt.get("successor_candidate_sha") is not None
        or utc_iso(successor_opened_at) != receipt.get("successor_opened_at")
    ):
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has invalid frozen successor identity"
        )


async def _find_cut_by_idempotency_key(
    session: AsyncSession,
    project_id: uuid.UUID,
    idempotency_key: str,
    *,
    request_fingerprint: str | None = None,
) -> VersionCutOutcome | None:
    """Read and validate one frozen prior cut; never infer from a key alone."""

    rows = list(
        (
            (
                await session.execute(
                    select(ReleaseVersion)
                    .where(
                        ReleaseVersion.project_id == project_id,
                        ReleaseVersion.last_qualification[
                            "idempotency_key"
                        ].astext
                        == idempotency_key,
                    )
                    .order_by(ReleaseVersion.version_number.desc())
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise VersionCutIdempotencyConflict(
            "idempotency key is bound to more than one abandoned version"
        )
    prior = rows[0]
    if prior.status != "abandoned":
        raise VersionCutIdempotencyConflict(
            "idempotency key is bound to a release version with invalid status"
        )
    receipt = _cut_receipt_from_version(prior)
    if receipt is None:
        raise VersionCutIdempotencyConflict(
            "idempotency key was used by a legacy cut without an exact receipt"
        )
    _validate_cut_receipt(
        receipt,
        project_id=project_id,
        idempotency_key=idempotency_key,
        prior=prior,
        request_fingerprint=request_fingerprint,
    )
    try:
        successor_id = uuid.UUID(str(receipt["successor_version_id"]))
    except (ValueError, TypeError) as exc:
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt has an invalid successor id"
        ) from exc
    successor = await session.get(
        ReleaseVersion,
        successor_id,
        populate_existing=True,
    )
    if (
        successor is None
        or successor.project_id != project_id
        or successor.version_number != receipt["successor_version_number"]
        or successor.version_number != prior.version_number + 1
        or successor.base_sha != receipt.get("successor_base_sha")
        or utc_iso(successor.opened_at) != receipt.get("successor_opened_at")
    ):
        raise VersionCutIdempotencyConflict(
            "stored version-cut receipt does not match its successor row"
        )
    version_cut_followup_projection(prior, receipt)
    return VersionCutOutcome(
        abandoned=prior,
        successor=successor,
        receipt=receipt,
        replayed=True,
    )


async def resolve_version_cut_integration_identity(
    session: AsyncSession,
    project: Project | None,
) -> VersionCutIntegrationIdentity:
    """Resolve the exact live integration head or fail closed."""

    if project is None:
        raise VersionCutIdentityUnavailable("project identity is unavailable")
    try:
        from app.github import get_branch_sha
        from app.release_promotion import _resolve_github_with_session
        from app.shared_state import get_project_integration_branch

        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            raise VersionCutIdentityUnavailable(
                "GitHub credentials are unavailable for exact version-cut identity"
            )
        token, owner, repo = resolved
        owner = str(owner or "").strip()
        repo = str(repo or "").strip()
        if not owner or not repo:
            raise VersionCutIdentityUnavailable(
                "repository identity is unavailable for exact version cut"
            )
        branch = str(get_project_integration_branch(project) or "").strip()
        if not branch:
            raise VersionCutIdentityUnavailable(
                "integration branch is unavailable for exact version-cut identity"
            )
        raw_sha = await get_branch_sha(token, owner, repo, branch, live=True)
    except VersionCutIdentityUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - classified fail-closed boundary
        raise VersionCutIdentityUnavailable(
            f"live integration identity is unavailable: {type(exc).__name__}"
        ) from exc
    sha = str(raw_sha or "").strip().lower()
    if not _FULL_SHA_RE.fullmatch(sha):
        raise VersionCutIdentityUnavailable(
            "live integration identity did not return a full commit SHA"
        )
    return VersionCutIntegrationIdentity(
        owner=owner,
        repo=repo,
        branch=branch,
        head_sha=sha,
        observed_at=_utcnow(),
    )


def _version_identity(version: ReleaseVersion | None) -> dict[str, Any]:
    if version is None:
        return {"version": None}
    return {
        "version": {
            "id": str(version.id),
            "version_number": version.version_number,
            "phase": version.phase,
            "status": version.status,
            "base_sha": version.base_sha,
            "candidate_sha": version.candidate_sha,
        }
    }


async def get_version_cut_context(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Return uncached exact cut identity or a frozen receipt for one key."""

    key = (
        _normalize_idempotency_key(idempotency_key)
        if idempotency_key is not None
        else None
    )
    if key is not None:
        replay = await _find_cut_by_idempotency_key(session, project_id, key)
        if replay is not None:
            return {
                "status": "committed",
                "replayed": True,
                "receipt": replay.receipt,
                "followups": version_cut_followup_projection(
                    replay.abandoned,
                    replay.receipt,
                ),
            }

    project = await session.get(Project, project_id, populate_existing=True)
    open_version = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "open",
                )
                .order_by(ReleaseVersion.opened_at.desc())
                .limit(1)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if open_version is None:
        raise VersionCutPreconditionFailed(
            "no open release version exists",
            current={"version": None},
        )
    identity = await resolve_version_cut_integration_identity(session, project)
    return {
        "status": "ready",
        "replayed": False,
        **_version_identity(open_version),
        "integration": identity.projection(),
    }


async def abandon_current_version(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    reason: str | None,
    operator_id: str,
    operator_display: str | None = None,
    idempotency_key: str,
    expected_version_id: uuid.UUID,
    expected_version_number: int,
    expected_integration_sha: str,
    expected_integration_identity: str,
) -> VersionCutOutcome:
    """Atomically cut one exact version after one exact live-head observation."""

    key = _normalize_idempotency_key(idempotency_key)
    version_id = uuid.UUID(str(expected_version_id))
    version_number = int(expected_version_number)
    if version_number < 1:
        raise ValueError("expected_version_number must be at least 1")
    expected_sha = _normalize_expected_sha(expected_integration_sha)
    expected_identity = str(expected_integration_identity or "").strip().lower()
    truncated_reason = _normalized_reason(reason)
    display_actor = str(operator_display or operator_id)
    fingerprint = _cut_request_fingerprint(
        project_id=project_id,
        operator_id=operator_id,
        reason=truncated_reason,
        expected_version_id=version_id,
        expected_version_number=version_number,
        expected_integration_sha=expected_sha,
        expected_integration_identity=expected_identity,
    )

    replay = await _find_cut_by_idempotency_key(
        session,
        project_id,
        key,
        request_fingerprint=fingerprint,
    )
    if replay is not None:
        return replay

    project = await session.get(Project, project_id, populate_existing=True)
    from app.shared_state import get_project_integration_branch

    observed_project_config = (
        str(project.github_owner or "").strip() if project is not None else "",
        str(project.github_repo or "").strip() if project is not None else "",
        str(get_project_integration_branch(project) or "").strip()
        if project is not None
        else "",
    )
    identity = await resolve_version_cut_integration_identity(session, project)
    if (
        identity.head_sha != expected_sha
        or identity.identity_token() != expected_identity
    ):
        raise VersionCutPreconditionFailed(
            "integration identity changed after version-cut preflight",
            current={"integration": identity.projection()},
        )

    # Qualification, deferral, and intent writers use Project→Version as the
    # canonical release authority order. Serialize the cut on the same project
    # row before taking its exact Version row; promotion follows this order too.
    locked_project = (
        await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_project is None:
        raise VersionCutPreconditionFailed(
            "project disappeared before version cut",
            current={"version": None},
        )
    replay = await _find_cut_by_idempotency_key(
        session,
        project_id,
        key,
        request_fingerprint=fingerprint,
    )
    if replay is not None:
        return replay

    locked = (
        (
            await session.execute(
                select(ReleaseVersion)
                .where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.status == "open",
                    ReleaseVersion.id == version_id,
                    ReleaseVersion.version_number == version_number,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if locked is None:
        # A simultaneous same-key caller may have committed while this caller
        # waited for the project/version lock. Reconcile the durable receipt
        # before reporting a stale precondition; never cut the successor.
        replay = await _find_cut_by_idempotency_key(
            session,
            project_id,
            key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay
        current = (
            (
                await session.execute(
                    select(ReleaseVersion)
                    .where(
                        ReleaseVersion.project_id == project_id,
                        ReleaseVersion.status == "open",
                    )
                    .order_by(ReleaseVersion.opened_at.desc())
                    .limit(1)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        raise VersionCutPreconditionFailed(
            "open release version changed after version-cut preflight",
            current=_version_identity(current),
        )

    locked_branch = str(get_project_integration_branch(locked_project) or "").strip()
    locked_owner = str(locked_project.github_owner or "").strip()
    locked_repo = str(locked_project.github_repo or "").strip()
    if (locked_owner, locked_repo, locked_branch) != observed_project_config:
        raise VersionCutPreconditionFailed(
            "repository configuration changed after version-cut preflight",
            current={
                "integration": {
                    "owner": str(locked_owner or "").strip() or None,
                    "repo": str(locked_repo or "").strip() or None,
                    "branch": locked_branch or None,
                }
            },
        )

    from app.qualification_state_resolver import resolve_promotion_blocking_result
    from app.release_version import VersionCutBlockedByPromotion, open_version

    promotion = await resolve_promotion_blocking_result(session, locked)
    if promotion is not None:
        raise VersionCutBlockedByPromotion(
            promotion.get("promotion_pr_number"),
            promotion.get("promotion_pr_url"),
            promotion_freshness=promotion,
        )

    final_identity = await resolve_version_cut_integration_identity(
        session,
        locked_project,
    )
    if (
        final_identity.head_sha != expected_sha
        or final_identity.identity_token() != expected_identity
    ):
        raise VersionCutPreconditionFailed(
            "integration identity changed under the version-cut lock",
            current={"integration": final_identity.projection()},
        )
    identity = final_identity

    now = _utcnow()
    next_version = await open_version(
        session,
        project_id,
        version_number=locked.version_number + 1,
        base_sha=expected_sha,
        phase="build",
        goal=None,
    )
    receipt = {
        "schema_version": _CUT_RECEIPT_SCHEMA_VERSION,
        "status": "committed",
        "idempotency_key": key,
        "request_fingerprint": fingerprint,
        "project_id": str(project_id),
        "operator_id": str(operator_id),
        "operator_display": display_actor,
        "reason": truncated_reason,
        "expected_version_id": str(version_id),
        "expected_version_number": version_number,
        "expected_integration_sha": expected_sha,
        "expected_integration_identity": expected_identity,
        "observed_repository_owner": identity.owner,
        "observed_repository_name": identity.repo,
        "observed_integration_branch": identity.branch,
        "observed_integration_sha": identity.head_sha,
        "observed_at": utc_iso(identity.observed_at),
        "abandoned_version_id": str(locked.id),
        "abandoned_version_number": locked.version_number,
        "abandoned_status": "abandoned",
        "abandoned_base_sha": locked.base_sha,
        "abandoned_candidate_sha": locked.candidate_sha,
        "abandoned_opened_at": utc_iso(locked.opened_at),
        "successor_version_id": str(next_version.id),
        "successor_version_number": next_version.version_number,
        "successor_status": "open",
        "successor_phase": "build",
        "successor_goal": None,
        "successor_base_sha": next_version.base_sha,
        "successor_candidate_sha": None,
        "successor_opened_at": utc_iso(next_version.opened_at),
        "committed_at": utc_iso(now),
    }
    record = dict(locked.last_qualification or {})
    record.update(
        {
            "status": "abandoned",
            "reason": truncated_reason,
            "operator": display_actor,
            "operator_id": str(operator_id),
            "abandoned_at": receipt["committed_at"],
            "idempotency_key": key,
            "version_cut_receipt": receipt,
            "version_cut_followups": {
                "schema_version": _CUT_FOLLOWUP_SCHEMA_VERSION,
                "request_fingerprint": fingerprint,
                "status": "pending",
                "attempts": 0,
                "created_at": utc_iso(now),
                "last_attempt_at": None,
                "completed_at": None,
                "last_error": None,
                "claim_token": None,
                "claim_expires_at": None,
            },
        }
    )
    locked.last_qualification = record
    flag_modified(locked, "last_qualification")
    human = (
        f"[operator:{display_actor}] abandoned: {truncated_reason}"
        if truncated_reason
        else f"[operator:{display_actor}] abandoned"
    )
    locked.phase_reasoning = human[:_CUT_REASON_MAX]
    locked.status = "abandoned"
    session.add(locked)

    # This high-signal receipt is a DB-local, idempotent write in the
    # caller-owned transaction. It is part of the exact cut: swallowing a
    # failure here would let receipt-first replay hide a missing terminal event
    # forever. All remote/separate-session follow-ups still run after commit.
    from app.notification_dispatch import post_release_version_abandoned_message

    await post_release_version_abandoned_message(
        session,
        project_id,
        version_id=locked.id,
        version_number=locked.version_number,
        successor_version_number=next_version.version_number,
        operator_display=display_actor,
        operator_id=str(operator_id),
        reason=truncated_reason,
    )

    return VersionCutOutcome(
        abandoned=locked,
        successor=next_version,
        receipt=receipt,
        replayed=False,
    )


async def _update_version_cut_followup_marker(
    receipt: dict[str, Any],
    *,
    begin_attempt: bool = False,
    completed: bool = False,
    error: str | None = None,
    claim_token: str | None = None,
) -> str:
    """CAS one durable follow-up attempt marker against the immutable receipt."""

    from app.database import AsyncSessionLocal

    fingerprint = str(receipt.get("request_fingerprint") or "")
    try:
        version_id = uuid.UUID(str(receipt["abandoned_version_id"]))
        project_id = uuid.UUID(str(receipt["project_id"]))
        key = _normalize_idempotency_key(str(receipt["idempotency_key"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise VersionCutIdempotencyConflict(
            "version-cut follow-up receipt identity is invalid"
        ) from exc

    async with AsyncSessionLocal() as marker_session:
        row = await lock_project_then_version(marker_session, version_id)
        if row is None or row.project_id != project_id:
            raise VersionCutIdempotencyConflict(
                "version-cut follow-up abandoned row identity is invalid"
            )
        stored_receipt = _cut_receipt_from_version(row)
        if stored_receipt is None:
            raise VersionCutIdempotencyConflict(
                "version-cut follow-up receipt is missing"
            )
        _validate_cut_receipt(
            stored_receipt,
            project_id=project_id,
            idempotency_key=key,
            prior=row,
            request_fingerprint=fingerprint,
        )
        if stored_receipt != receipt:
            raise VersionCutIdempotencyConflict(
                "version-cut follow-up receipt changed after commit"
            )
        qualification = dict(row.last_qualification or {})
        version_cut_followup_projection(row, stored_receipt)
        marker = dict(qualification.get("version_cut_followups") or {})
        if marker.get("status") == "complete":
            return "complete"
        now = _utcnow()
        now_wire = utc_iso(now)
        if begin_attempt:
            existing_token = marker.get("claim_token")
            existing_expiry = marker.get("claim_expires_at")
            if existing_token is not None and existing_expiry is not None:
                expiry = _coerce_naive_utc(
                    _canonical_utc_timestamp(
                        existing_expiry,
                        "follow-up claim_expires_at",
                    )
                )
                if expiry > now:
                    return "busy"
            new_claim = str(uuid.uuid4())
            marker.update(
                {
                    "status": "pending",
                    "attempts": int(marker.get("attempts") or 0) + 1,
                    "last_attempt_at": now_wire,
                    "last_error": None,
                    "claim_token": new_claim,
                    "claim_expires_at": utc_iso(
                        now + timedelta(seconds=_CUT_FOLLOWUP_LEASE_SECONDS)
                    ),
                }
            )
            result = f"claim:{new_claim}"
        elif claim_token is None or marker.get("claim_token") != claim_token:
            return "lost"
        elif completed:
            marker.update(
                {
                    "status": "complete",
                    "completed_at": now_wire,
                    "last_error": None,
                    "claim_token": None,
                    "claim_expires_at": None,
                }
            )
            result = "complete"
        else:
            marker.update(
                {
                    "status": "pending",
                    "last_error": str(error)[:500] if error else None,
                    "claim_token": None,
                    "claim_expires_at": None,
                }
            )
            result = "pending"
        qualification["version_cut_followups"] = marker
        row.last_qualification = qualification
        flag_modified(row, "last_qualification")
        marker_session.add(row)
        await marker_session.commit()
        return result


async def _run_version_cut_followups(
    receipt: dict[str, Any],
) -> VersionCutFollowupResult:
    """Replay one convergent follow-up bundle and durably settle its marker."""

    try:
        state = await _update_version_cut_followup_marker(
            receipt,
            begin_attempt=True,
        )
        if state == "complete":
            return VersionCutFollowupResult(complete=True, executed=False)
        if not state.startswith("claim:"):
            return VersionCutFollowupResult(complete=False, executed=False)
        claim_token = state.removeprefix("claim:")
    except Exception as exc:  # noqa: BLE001 - pending marker remains a backstop
        logger.warning("version-cut follow-up claim failed: %s", exc)
        return VersionCutFollowupResult(complete=False, executed=False)

    project_id = uuid.UUID(receipt["project_id"])
    version_number = int(receipt["abandoned_version_number"])
    opened_at = _coerce_naive_utc(
        datetime.fromisoformat(receipt["abandoned_opened_at"])
    )
    closed_at = _coerce_naive_utc(datetime.fromisoformat(receipt["committed_at"]))

    try:
        from app.database import AsyncSessionLocal
        from app.directions_repo import activate_eligible_staged_directions

        async with AsyncSessionLocal() as followup_session:
            await activate_eligible_staged_directions(
                followup_session,
                project_id,
                version_number,
                terminal_status="abandoned",
            )
    except Exception as exc:  # noqa: BLE001 - durable marker retains liveness
        error = f"staged_activation:{type(exc).__name__}: {exc}"
        logger.warning(
            "version-cut staged activation failed after v%d commit: %s",
            version_number,
            exc,
        )
        with contextlib.suppress(Exception):
            await _update_version_cut_followup_marker(
                receipt,
                error=error,
                claim_token=claim_token,
            )
        return VersionCutFollowupResult(complete=False, executed=True)

    try:
        from app.database import AsyncSessionLocal
        from app.strategic_trigger import request_strategic_refresh

        async with AsyncSessionLocal() as followup_session:
            armed = await request_strategic_refresh(
                followup_session,
                project_id,
                f"version_abandoned:v{version_number}",
            )
            if not armed:
                raise RuntimeError("strategic refresh marker was not persisted")
            await followup_session.commit()
    except Exception as exc:  # noqa: BLE001 - durable marker retains liveness
        error = f"strategic_refresh:{type(exc).__name__}: {exc}"
        logger.warning(
            "version-cut strategic refresh failed after v%d commit: %s",
            version_number,
            exc,
        )
        with contextlib.suppress(Exception):
            await _update_version_cut_followup_marker(
                receipt,
                error=error,
                claim_token=claim_token,
            )
        return VersionCutFollowupResult(complete=False, executed=True)

    try:
        from app.database import AsyncSessionLocal
        from app.lane_baseline_snapshot import persist_version_lane_baseline

        async with AsyncSessionLocal() as followup_session:
            baseline = await persist_version_lane_baseline(
                followup_session,
                project_id=project_id,
                version_number=version_number,
                opened_at=opened_at,
                closed_at=closed_at,
                terminal_status="abandoned",
            )
            if baseline is None:
                raise RuntimeError("lane baseline did not persist")
            await followup_session.commit()
    except Exception as exc:  # noqa: BLE001 - durable marker retains liveness
        error = f"lane_baseline:{type(exc).__name__}: {exc}"
        logger.warning(
            "version-cut lane baseline failed after v%d commit: %s",
            version_number,
            exc,
        )
        with contextlib.suppress(Exception):
            await _update_version_cut_followup_marker(
                receipt,
                error=error,
                claim_token=claim_token,
            )
        return VersionCutFollowupResult(complete=False, executed=True)

    try:
        from app.database import AsyncSessionLocal
        from app.message_supersession import supersede_stale_messages_on_version_close

        async with AsyncSessionLocal() as followup_session:
            summary = await supersede_stale_messages_on_version_close(
                followup_session,
                project_id=project_id,
                closed_version_number=version_number,
                closed_version_opened_at=opened_at,
                terminal_status="abandoned",
                now=closed_at,
            )
            if summary.get("acted"):
                await followup_session.commit()
        for broadcast_project_id, payload in summary.get("broadcasts") or []:
            try:
                from app.broadcast import broadcast_to_project

                await broadcast_to_project(
                    broadcast_project_id,
                    "messages_superseded",
                    payload,
                )
            except Exception as broadcast_exc:  # noqa: BLE001 - state committed
                logger.warning(
                    "version-cut supersession broadcast failed after v%d: %s",
                    version_number,
                    broadcast_exc,
                )
    except Exception as exc:  # noqa: BLE001 - durable marker retains liveness
        error = f"message_supersession:{type(exc).__name__}: {exc}"
        logger.warning(
            "version-cut message supersession failed after v%d commit: %s",
            version_number,
            exc,
        )
        with contextlib.suppress(Exception):
            await _update_version_cut_followup_marker(
                receipt,
                error=error,
                claim_token=claim_token,
            )
        return VersionCutFollowupResult(complete=False, executed=True)

    try:
        completion_state = await _update_version_cut_followup_marker(
            receipt,
            completed=True,
            claim_token=claim_token,
        )
        return VersionCutFollowupResult(
            complete=completion_state == "complete",
            executed=True,
        )
    except Exception as exc:  # noqa: BLE001 - all effects converge on retry
        logger.warning(
            "version-cut follow-up completion receipt failed after v%d: %s",
            version_number,
            exc,
        )
        return VersionCutFollowupResult(complete=False, executed=True)


async def run_version_cut_followups(
    outcome: VersionCutOutcome,
) -> VersionCutFollowupResult:
    """Run or replay durable idempotent follow-ups only after cut commit."""

    return await _run_version_cut_followups(dict(outcome.receipt))


async def sweep_pending_version_cut_followups(
    session: AsyncSession,
    *,
    batch: int = 20,
) -> int:
    """Periodically replay pending exact-cut follow-ups after process loss."""

    rows = list(
        (
            (
                await session.execute(
                    select(ReleaseVersion)
                    .where(
                        ReleaseVersion.status == "abandoned",
                        ReleaseVersion.last_qualification[
                            "version_cut_followups"
                        ]["status"].as_string()
                        == "pending",
                    )
                    .order_by(ReleaseVersion.opened_at.asc())
                    .limit(max(1, min(int(batch), 100)))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    )
    candidates = [
        (row.id, receipt)
        for row in rows
        if (receipt := _cut_receipt_from_version(row)) is not None
    ]
    completed = 0
    for version_id, receipt in candidates:
        try:
            result = await _run_version_cut_followups(receipt)
            if result.complete:
                completed += 1
        except Exception as exc:  # noqa: BLE001 - isolate each durable receipt
            logger.warning(
                "version-cut follow-up sweep failed for version %s: %s",
                version_id,
                exc,
            )
    return completed
