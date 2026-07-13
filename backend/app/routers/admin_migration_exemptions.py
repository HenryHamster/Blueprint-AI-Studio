"""Operator-facing admin router for runtime migration-divergence exemptions.

Background. Blueprint's pre-merge / qualification migration guard hard-blocks any
Alembic revision-identity divergence unless the divergence is on the *authorized*
list. That list is the immutable baked-in floor ``AUTHORIZED_DIVERGENCES`` UNION a
DB-backed, operator-approved runtime store (the ``migration_divergence_exemptions``
runtime-config key), resolved at gate time by
``migration_reconciliation.effective_authorized_divergences()``. The runtime store
lets an operator authorize a NEW divergence WITHOUT a redeploy — the single most
recurring release-machinery self-reference case.

This module is the sanctioned WRITE path for that store: the audited channel to
add / remove a runtime exemption. Because every exemption authorizes a bypass of a
migration SAFETY gate, the audit trail (who / when / why) is a first-class
requirement — every add and remove records the acting operator and, for adds, a
required human justification. The endpoints:

* ``GET  /api/admin/migration-exemptions`` — the operator's read of what the
  deployed gate will currently honor: baked entries (``source="baked"``) plus the
  runtime store (``source="runtime"``, with audit metadata).
* ``POST /api/admin/migration-exemptions`` — add a runtime exemption. Operator
  identity is captured server-side from the auth dependency; a client-supplied
  actor / timestamp is never trusted.
* ``DELETE /api/admin/migration-exemptions`` — remove a runtime exemption by its
  match key. A baked-in entry is the immutable floor and can never be deleted.

Purely additive: a new router and new endpoints. It only CONSUMES already-exported
symbols from ``migration_reconciliation`` / ``runtime_config_state`` — it changes
no existing endpoint signature or response shape.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth import require_admin
from app.database import get_session
from app.migration_reconciliation import (
    AUTHORIZED_DIVERGENCES,
    RUNTIME_EXEMPTIONS_CONFIG_KEY,
    effective_authorized_divergences,
    normalize_runtime_divergence_exemption,
)
from app.models import DeploymentState
from app.shared_state import limiter, runtime_config as _runtime_config, runtime_config_lock
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Alembic revision ids in this repo are short slugs (e.g. "0143", "0458"). Keep the
# shape check lenient but non-empty and free of whitespace/path characters so a
# malformed id can't slip a garbage row into the safety-gate store.
_REVISION_SHAPE = re.compile(r"^[0-9A-Za-z_.-]{1,64}$")

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-migration-exemptions"],
    dependencies=[Depends(require_admin)],
)


def _match_key(entry: dict) -> tuple[str, str, str]:
    """The identity match key of an exemption: (revision, deployed, integration).

    This mirrors the authorization matching in
    ``rollup_migration_validation._divergence_record_is_authorized`` (revision id +
    the identity pair) so "does this key already exist" here agrees with "is this
    divergence authorized" at the gate.
    """
    return (
        str(entry.get("revision", "")).strip(),
        str(entry.get("deployed_identity", "")).strip(),
        str(entry.get("integration_identity", "")).strip(),
    )


def _baked_keys() -> set[tuple[str, str, str]]:
    return {
        (d.revision, d.deployed_identity, d.integration_identity)
        for d in AUTHORIZED_DIVERGENCES
    }


def _effective_rows() -> list[dict]:
    """The effective exemption list as JSON-serializable rows, each tagged with its
    ``source`` (``baked`` vs ``runtime``) and, for runtime rows, its audit metadata.
    """
    rows: list[dict] = []
    for div in effective_authorized_divergences():
        rows.append(
            {
                "source": "runtime" if div.is_runtime else "baked",
                "revision": div.revision,
                "parent_revision": div.parent_revision,
                "deployed_identity": div.deployed_identity,
                "integration_identity": div.integration_identity,
                "created_table": div.created_table,
                "integration_revision": div.integration_revision,
                # Audit metadata is empty for the baked floor and populated only for
                # operator-approved runtime exemptions.
                "added_by": div.added_by,
                "added_at": div.added_at,
                "reason": div.reason,
            }
        )
    return rows


async def _persist_runtime_exemptions(
    session: AsyncSession, exemptions: list[dict]
) -> None:
    """Persist the runtime exemption list into ``DeploymentState.runtime_config_json``
    so it survives Cloud Run restarts (config-sync rehydrates it on boot).

    Uses the same read-merge-``flag_modified`` persistence pattern the other
    runtime-config writers use (e.g. admin_project_config) rather than overwriting
    the whole JSON blob, so a concurrent unrelated key is never clobbered. The
    caller holds ``runtime_config_lock`` around the read-modify-write. When no
    ``DeploymentState`` row exists yet (fresh install / unit tests) the change lives
    in the in-memory config only; the next deploy bootstrap creates the row.
    """
    state = (
        await session.execute(select(DeploymentState).where(DeploymentState.id == 1))
    ).scalar_one_or_none()
    if state is None:
        return
    existing_json = dict(state.runtime_config_json or {})
    existing_json[RUNTIME_EXEMPTIONS_CONFIG_KEY] = exemptions
    state.runtime_config_json = existing_json
    flag_modified(state, "runtime_config_json")
    session.add(state)
    await session.commit()


def _runtime_store() -> list[dict]:
    """Current in-memory runtime exemption store as a list of dicts (never None)."""
    raw = _runtime_config.get(RUNTIME_EXEMPTIONS_CONFIG_KEY)
    return list(raw) if isinstance(raw, (list, tuple)) else []


@router.get("/migration-exemptions")
async def list_migration_exemptions(
    current_user: dict = Depends(require_admin),
):
    """List the EFFECTIVE migration-divergence exemptions the deployed gate honors.

    Returns baked-in entries (``source="baked"``) plus the operator-approved runtime
    store (``source="runtime"`` with audit metadata). This is the operator's read of
    exactly what the migration safety gate will currently wave through.
    """
    return {"exemptions": _effective_rows()}


@router.post("/migration-exemptions", status_code=201)
@limiter.limit("30/minute")
async def add_migration_exemption(
    request: Request,
    payload: dict,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Add an operator-approved runtime migration-divergence exemption.

    Accepts the ``AuthorizedDivergence`` match fields plus a REQUIRED non-empty
    ``reason``. The acting operator identity (``added_by``) and timestamp
    (``added_at``) are stamped server-side from the auth dependency — a
    client-supplied actor / timestamp is ignored. Duplicates of a baked entry, or of
    an existing runtime entry, are rejected as a 409 no-op.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    def _field(key: str) -> str:
        val = payload.get(key)
        return str(val).strip() if val is not None else ""

    revision = _field("revision")
    parent_revision = _field("parent_revision")
    deployed_identity = _field("deployed_identity")
    integration_identity = _field("integration_identity")
    created_table = _field("created_table")
    integration_revision = _field("integration_revision")
    reason = _field("reason")

    # Required-field + shape validation. The runtime store gates a deploy-safety
    # check, so this is strict: an incomplete or malformed record is a 400, never a
    # silently-stored no-op.
    missing = [
        name
        for name, value in (
            ("revision", revision),
            ("parent_revision", parent_revision),
            ("deployed_identity", deployed_identity),
            ("integration_identity", integration_identity),
            ("reason", reason),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required field(s): {sorted(missing)}",
        )
    for name, value in (
        ("revision", revision),
        ("parent_revision", parent_revision),
    ):
        if not _REVISION_SHAPE.match(value):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} {value!r} is not a valid revision id "
                    "(expected a short alphanumeric slug)"
                ),
            )

    # Operator identity is authoritative and server-stamped. Never trust a
    # client-supplied added_by / added_at.
    actor = str(
        current_user.get("email") or current_user.get("user_id") or ""
    ).strip()
    if not actor:
        raise HTTPException(
            status_code=403, detail="Could not resolve acting operator identity"
        )

    candidate = {
        "revision": revision,
        "parent_revision": parent_revision,
        "deployed_identity": deployed_identity,
        "integration_identity": integration_identity,
        "created_table": created_table,
        "integration_revision": integration_revision,
        "added_by": actor,
        "added_at": _utcnow().isoformat(),
        "reason": reason,
    }

    # Run the SAME fail-closed normalizer the rehydrate path and the live gate use,
    # so a record accepted here is exactly a record the gate will honor.
    normalized = normalize_runtime_divergence_exemption(candidate)
    if normalized is None:
        raise HTTPException(
            status_code=400,
            detail="Exemption failed validation (fields missing or malformed)",
        )

    key = _match_key(normalized)
    if key in _baked_keys():
        raise HTTPException(
            status_code=409,
            detail=(
                "Exemption exactly duplicates a baked-in authorized divergence; "
                "it is already honored and cannot be added as a runtime entry"
            ),
        )

    async with runtime_config_lock:
        store = _runtime_store()
        if any(_match_key(e) == key for e in store):
            raise HTTPException(
                status_code=409,
                detail="A runtime exemption with this identity match key already exists",
            )
        updated = store + [normalized]
        _runtime_config[RUNTIME_EXEMPTIONS_CONFIG_KEY] = updated
        try:
            await _persist_runtime_exemptions(session, updated)
        except Exception as exc:  # noqa: BLE001 — restore in-memory state on DB failure
            _runtime_config[RUNTIME_EXEMPTIONS_CONFIG_KEY] = store
            await session.rollback()
            logger.exception(
                "[migration-exemptions] DB persist failed; rolled back in-memory add"
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to persist exemption: {exc}"
            ) from exc

    logger.warning(
        "[migration-exemptions] operator %r ADDED runtime exemption revision=%r "
        "identities={%r,%r} reason=%r",
        actor,
        normalized["revision"],
        normalized["deployed_identity"],
        normalized["integration_identity"],
        normalized["reason"],
    )
    return {"exemption": {**normalized, "source": "runtime"}}


@router.delete("/migration-exemptions")
@limiter.limit("30/minute")
async def remove_migration_exemption(
    request: Request,
    revision: str = Query(..., description="Revision id of the exemption to remove"),
    deployed_identity: str = Query(..., description="Deployed identity of the pair"),
    integration_identity: str = Query(..., description="Integration identity of the pair"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Remove a runtime migration-divergence exemption by its match key.

    A baked-in entry is the immutable safety floor and can NEVER be deleted — an
    attempt to remove one is rejected. The removal is logged with the acting
    operator.
    """
    key = (
        str(revision).strip(),
        str(deployed_identity).strip(),
        str(integration_identity).strip(),
    )
    if not all(key):
        raise HTTPException(
            status_code=400,
            detail="revision, deployed_identity and integration_identity are required",
        )
    if key in _baked_keys():
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot delete a baked-in authorized divergence; the baked list is "
                "the immutable safety floor"
            ),
        )

    actor = str(
        current_user.get("email") or current_user.get("user_id") or ""
    ).strip()

    async with runtime_config_lock:
        store = _runtime_store()
        remaining = [e for e in store if _match_key(e) != key]
        if len(remaining) == len(store):
            raise HTTPException(
                status_code=404,
                detail="No runtime exemption matches the given identity key",
            )
        _runtime_config[RUNTIME_EXEMPTIONS_CONFIG_KEY] = remaining
        try:
            await _persist_runtime_exemptions(session, remaining)
        except Exception as exc:  # noqa: BLE001 — restore in-memory state on DB failure
            _runtime_config[RUNTIME_EXEMPTIONS_CONFIG_KEY] = store
            await session.rollback()
            logger.exception(
                "[migration-exemptions] DB persist failed; rolled back in-memory remove"
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to persist removal: {exc}"
            ) from exc

    logger.warning(
        "[migration-exemptions] operator %r REMOVED runtime exemption revision=%r "
        "identities={%r,%r}",
        actor or "unknown",
        key[0],
        key[1],
        key[2],
    )
    return {"removed": {"revision": key[0], "deployed_identity": key[1], "integration_identity": key[2]}}
