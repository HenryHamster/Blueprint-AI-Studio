"""Approved promotion-activation control (in-product approval stamp).

The in-product one-click activation path splits "approve" (an in-product admin
decision, cheap and safe to expose) from "execute" (the privileged host-side
activation, which stays on the control-plane host outside the API/worker
containers).  An administrator approves the CURRENT promoted, still-unconfirmed
version; the trusted control-plane host pulls that approval and executes the
already-existing root-owned ``sudo`` shim; the activation is then recorded BY
RECEIPT through the existing activation-receipt endpoint.

This module holds the approval-state helpers shared by the admin router and the
release-reconciliation receipt path.  It deliberately contains NO ``ssh`` /
``paramiko`` and no outbound-deploy capability: the backend may only record an
approval stamp; the privileged activation is PULLED and executed host-side.
"""

from __future__ import annotations


import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app import release_version

logger = logging.getLogger(__name__)
_MODULE_SIZE_GOVERNANCE_CAP: int = 241


DEFAULT_ACTIVATION_ATTEMPTS = 3
DEFAULT_ACTIVATION_BACKOFF_SECONDS = (5.0, 10.0)


class ActivationPhaseFailure(RuntimeError):
    """A bounded activation phase exhausted its retry budget."""

    def __init__(self, phase: str, attempts: int, final_exception: Exception):
        self.phase = phase
        self.attempts = attempts
        self.final_exception = final_exception
        super().__init__(
            f"{phase} failed after {attempts} attempts: {final_exception}"
        )


def retry_activation_phase(
    phase: str,
    action: Callable[[], Any],
    *,
    max_attempts: int = DEFAULT_ACTIVATION_ATTEMPTS,
    backoff_seconds: tuple[float, ...] = DEFAULT_ACTIVATION_BACKOFF_SECONDS,
    retry_if: Callable[[Exception], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    before_attempt: Callable[[int], None] | None = None,
) -> Any:
    """Run one activation phase with bounded retries and real backoff.

    ``retry_if`` lets the caller keep non-transient failures fail-fast.  A
    retryable failure is wrapped in :class:`ActivationPhaseFailure` when the
    finite budget is exhausted so the caller can persist a truthful terminal
    result instead of turning it into an indefinite poll or timeout.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if any(delay <= 0 for delay in backoff_seconds):
        raise ValueError("activation retry backoff must be positive")

    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        if before_attempt is not None:
            before_attempt(attempts)
        try:
            return action()
        except Exception as exc:
            if retry_if is not None and not retry_if(exc):
                raise
            if attempts >= max_attempts:
                raise ActivationPhaseFailure(phase, attempts, exc) from exc
            delay_index = min(attempts - 1, len(backoff_seconds) - 1)
            delay = backoff_seconds[delay_index] if backoff_seconds else 0.0
            if delay <= 0:
                raise ValueError("activation retry backoff must not be empty") from exc
            sleep(delay)


def _approved_activation_mismatch(
    version: release_version.ReleaseVersion,
    effective_sha: str,
) -> str | None:
    """Return a rejection detail when an ACTIVE approval reports a different revision.

    An in-product activation approval authorizes EXACTLY the approved version's
    immutable ``promoted_sha``.  While that approval is still live (the version
    is not yet ``serving_confirmed``), a receipt reporting any other revision is
    a mismatch — a different or historical artifact — and must be rejected rather
    than confirmed.  Returns ``None`` (no rejection) when:

      * there is no approval (the legacy ``blueprint deploy`` CLI path — its
        built revision may legitimately be a descendant of ``promoted_sha``, so
        that path stays unchanged and backward compatible), or
      * the version is already ``serving_confirmed`` (the existing idempotency /
        recovery machinery governs re-POSTs), or
      * the reported revision equals the approved ``promoted_sha``.
    """
    if version.activation_requested_at is None:
        return None
    if version.serving_status == "serving_confirmed":
        return None
    if not version.promoted_sha:
        return None
    if effective_sha == version.promoted_sha:
        return None
    return (
        f"The approved activation for v{version.version_number} is bound to "
        f"promoted SHA {version.promoted_sha[:12]}...; reported revision "
        f"{effective_sha[:12] if effective_sha else '(none)'} does not match "
        "the approved artifact."
    )


async def _approved_activation_superseded(
    session,
    version: release_version.ReleaseVersion,
) -> bool:
    """True when an approved version is no longer the latest promoted version.

    An approval authorizes activation of the version that was CURRENT when the
    operator approved it.  Once a newer promotion has superseded it, executing
    that approval would activate a historical artifact — so the receipt path
    must reject it.  ``clear_deploy_failed`` and the attention layer already
    scope "latest promoted" the same way (highest-numbered ``status="promoted"``
    row).
    """
    from app import release_version
    from sqlalchemy import select

    latest = (
        await session.execute(
            select(release_version.ReleaseVersion)
            .where(
                release_version.ReleaseVersion.project_id == version.project_id,
                release_version.ReleaseVersion.status == "promoted",
            )
            .order_by(release_version.ReleaseVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return latest is not None and latest.id != version.id


async def _approved_activation_descendant(
    session,
    version: release_version.ReleaseVersion,
    effective_sha: str,
) -> bool:
    """Return whether *effective_sha* contains the approved promoted commit.

    The CLI validates this same relationship before it activates: its checked
    out revision may be a merge commit or other descendant of ``promoted_sha``
    rather than the exact promotion commit.  The approval guard must recognize
    that valid artifact without trusting an unverified mismatch.  Any missing
    credentials, unavailable project, API error, or indeterminate comparison
    fails closed so the receipt remains rejected.
    """
    if not version.promoted_sha or not effective_sha:
        return False

    from app.models import Project

    project = await session.get(Project, version.project_id)
    if project is None:
        return False

    try:
        from app import github
        from app.release_promotion import _resolve_github_with_session

        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return False
        token, owner, repo = resolved
        ancestry = await github.is_ancestor_of(
            token,
            owner,
            repo,
            version.promoted_sha,
            effective_sha,
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — fail closed on verification errors
        logger.warning(
            "[RELEASE_ACTIVATION] descendant verification failed for v%s (%s)",
            version.version_number,
            exc,
        )
        return False
    return ancestry is True


async def reject_stale_or_mismatched_approved_receipt(
    session,
    version: release_version.ReleaseVersion,
    effective_sha: str,
) -> str | None:
    """Return a 409 rejection detail for a receipt under a live approval, else None.

    An in-product approval authorizes the approved version's promoted SHA, and
    a CLI receipt may report a verified descendant of that SHA.  Only while
    that version is still the CURRENT promoted version is the approval live. A
    receipt reporting a diverged, behind, unrelated, or unverifiable revision
    — or a receipt once a newer promotion has superseded the approved version
    — is rejected.  Returns ``None`` when the receipt is acceptable: no approval
    (the legacy ``blueprint deploy`` CLI path is unchanged), the version is
    already ``serving_confirmed`` (idempotency / recovery governs re-POSTs), or
    the reported revision is the approved SHA or a verified descendant and no
    newer promotion has superseded the version.

    Applied whenever ``activation_requested_at`` is set and the version is not
    yet resolved — including a never-armed promotion whose ``serving_status`` is
    NULL — so the SHA-strictness guarantee is not conditioned on the version
    having been armed.
    """
    if version.activation_requested_at is None:
        return None
    if version.serving_status == "serving_confirmed":
        return None
    if await _approved_activation_superseded(session, version):
        return (
            f"The approved activation for v{version.version_number} is stale "
            f"— a newer promotion has superseded it. Approve the current "
            "promoted version instead."
        )
    mismatch = _approved_activation_mismatch(version, effective_sha)
    if mismatch is None:
        return None
    if await _approved_activation_descendant(session, version, effective_sha):
        return None
    return mismatch
