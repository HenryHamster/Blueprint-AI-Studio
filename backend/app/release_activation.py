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

from sqlalchemy import select

from app import release_version


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


async def reject_stale_or_mismatched_approved_receipt(
    session,
    version: release_version.ReleaseVersion,
    effective_sha: str,
) -> str | None:
    """Return a 409 rejection detail for a receipt under a live approval, else None.

    An in-product approval authorizes EXACTLY the approved version's promoted
    SHA, and only while that version is still the CURRENT promoted version.  A
    receipt reporting a different revision — or a receipt once a newer promotion
    has superseded the approved version — is rejected.  Returns ``None`` when the
    receipt is acceptable: no approval (the legacy ``blueprint deploy`` CLI path
    is unchanged), the version is already ``serving_confirmed`` (idempotency /
    recovery governs re-POSTs), or the reported revision equals the approved
    ``promoted_sha`` and no newer promotion has superseded the version.

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
    return _approved_activation_mismatch(version, effective_sha)
