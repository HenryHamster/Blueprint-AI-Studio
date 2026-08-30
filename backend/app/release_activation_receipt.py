"""Crash-durable activation receipt projection without provider I/O."""

from __future__ import annotations
from datetime import datetime
from typing import Any
from sqlalchemy import select

from app import release_version
from app.deploy_identity import deploy_repo_identity
from app.models import Project, ReleaseDeployArtifact
from app.release_artifacts import (
    BACKEND_REVISION_ARTIFACT_KIND,
    aggregate_required_artifact_status,
    apply_serving_decision,
    get_or_create_release_deploy_artifact,
    is_backend_revision_artifact,
    iter_release_artifact_providers,
    observable_release_artifact_providers,
)

_MODULE_SIZE_GOVERNANCE_CAP: int = 250


class ActivationReceiptConflict(ValueError):
    """An activation retry disagrees with the immutable durable receipt."""


def replay_activation_receipt(
    version: release_version.ReleaseVersion,
    *,
    revision: str | None,
    host: str | None,
    operator_id: str,
    operator_display: str | None = None,
) -> dict[str, Any] | None:
    """Validate and return an immutable receipt without mutating release state."""
    served_sha = (revision or version.promoted_sha or "").strip()
    if len(served_sha) != 40 or any(ch not in "0123456789abcdef" for ch in served_sha):
        raise ActivationReceiptConflict(
            "Activation receipt has no valid 40-character served revision."
        )
    qual = dict(version.last_qualification or {})
    if "activation_receipt" not in qual:
        return None
    existing = qual.get("activation_receipt")
    if not isinstance(existing, dict) or not isinstance(existing.get("activated_at"), str):
        raise ActivationReceiptConflict(
            "The existing activation receipt is malformed; refusing to overwrite "
            "immutable activation provenance."
        )
    try:
        datetime.fromisoformat(existing["activated_at"])
    except ValueError as exc:
        raise ActivationReceiptConflict(
            "The existing activation receipt timestamp is malformed."
        ) from exc
    display = str(operator_display or operator_id)
    if "operator_id" in existing:
        if not all(
            isinstance(existing.get(field), str) and bool(existing[field])
            for field in ("operator_id", "operator_display", "operator")
        ):
            raise ActivationReceiptConflict(
                "The existing activation receipt actor is malformed."
            )
        actor_matches = existing["operator_id"] == operator_id
    else:
        actor_matches = existing.get("operator") == display
    if (
        existing.get("revision") != served_sha
        or existing.get("host") != host
        or not actor_matches
        or existing.get("mode") not in {"approved_activation", "cli_activation"}
    ):
        raise ActivationReceiptConflict(
            "Activation receipt conflicts with the immutable receipt already "
            "recorded for this release version."
        )
    if (
        version.serving_status == "serving_confirmed"
        and version.served_sha != served_sha
    ):
        raise ActivationReceiptConflict(
            "Activation receipt replay conflicts with current serving state."
        )
    return {
        "mode": existing["mode"],
        "serving_status": version.serving_status or "unconfirmed",
        "served_sha": version.served_sha or served_sha,
    }


async def record_activation_receipt(
    session,
    version: release_version.ReleaseVersion,
    *,
    revision: str | None,
    host: str | None,
    operator_id: str,
    operator_display: str | None = None,
    now: datetime,
) -> dict[str, Any]:
    """Record backend evidence and seed required rows without provider I/O."""
    served_sha = (revision or version.promoted_sha or "").strip()
    replay = replay_activation_receipt(
        version,
        revision=revision,
        host=host,
        operator_id=operator_id,
        operator_display=operator_display,
    )
    if replay is not None:
        return replay
    mode = (
        "approved_activation"
        if version.activation_requested_at
        else "cli_activation"
    )
    display = str(operator_display or operator_id)
    effect_identity = {
        "mode": mode,
        "revision": served_sha or None,
        "host": host,
        "activation_requested_at": (
            version.activation_requested_at.isoformat()
            if version.activation_requested_at
            else None
        ),
        "activation_requested_by": version.activation_requested_by,
    }
    qual = dict(version.last_qualification or {})
    receipt_block = {
        **effect_identity,
        "operator_id": operator_id,
        "operator_display": display,
        "operator": display,
        "activated_at": now.isoformat(),
    }
    effect_at = now
    qual["activation_receipt"] = receipt_block
    version.last_qualification = qual
    session.add(version)

    project = await session.get(Project, version.project_id)
    observable_providers = observable_release_artifact_providers(
        project, deploy_repo_identity()
    )
    provider_rows: list[tuple[object, ReleaseDeployArtifact]] = []
    observable_kinds: set[str] = set()
    backend_artifact: ReleaseDeployArtifact | None = None
    for provider in observable_providers:
        artifact, _created = await get_or_create_release_deploy_artifact(
            session,
            version,
            provider.artifact_kind,
            confirmation_deadline=version.deploy_confirm_deadline,
        )
        provider_rows.append((provider, artifact))
        observable_kinds.add(str(provider.artifact_kind))
        if is_backend_revision_artifact(provider.artifact_kind):
            backend_artifact = artifact

    required_provider_map = {
        str(provider.artifact_kind): provider
        for provider in iter_release_artifact_providers()
        if provider.required
    }
    unobservable_required = set(required_provider_map) - observable_kinds
    if unobservable_required:
        historical_rows = (
            (
                await session.execute(
                    select(ReleaseDeployArtifact).where(
                        ReleaseDeployArtifact.version_id == version.id,
                        ReleaseDeployArtifact.artifact_kind.in_(
                            unobservable_required
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        provider_rows.extend(
            (required_provider_map[str(artifact.artifact_kind)], artifact)
            for artifact in historical_rows
        )

    if backend_artifact is None:
        backend_artifact, _created = await get_or_create_release_deploy_artifact(
            session,
            version,
            BACKEND_REVISION_ARTIFACT_KIND,
            confirmation_deadline=version.deploy_confirm_deadline,
        )
        backend_provider = next(
            (
                provider
                for provider in iter_release_artifact_providers()
                if is_backend_revision_artifact(provider.artifact_kind)
            ),
            None,
        )
        if backend_provider is not None:
            provider_rows.insert(0, (backend_provider, backend_artifact))
    apply_serving_decision(
        backend_artifact,
        ("serving_confirmed", served_sha, effect_at, None),
        now=now,
    )
    session.add(backend_artifact)

    aggregate_status, aggregate_reason = aggregate_required_artifact_status(
        provider_rows
    )
    authorship_plan = None
    if aggregate_status == "serving_confirmed":
        # Lazy import keeps this receipt-only module independent at import time.
        from app.release_serving_confirmation import _recover_serving_confirmed

        authorship_plan = await _recover_serving_confirmed(
            session,
            version,
            served_sha,
            effect_at,
            now,
            confirmed_reason_code=backend_artifact.reason_code,
        )
    result = {
        "mode": mode,
        "serving_status": aggregate_status,
        "served_sha": served_sha,
    }
    if aggregate_reason is not None:
        result["reason_code"] = aggregate_reason
    if authorship_plan is not None:
        result["_authorship_plan"] = authorship_plan
    return result


_record_activation_receipt = record_activation_receipt
