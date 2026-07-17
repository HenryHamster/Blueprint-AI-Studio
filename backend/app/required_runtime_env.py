"""Declarative manifests of environment variables the app requires.

Two manifests are maintained here:

BOOT contract (:data:`REQUIRED_RUNTIME_ENV`)
    Hard requirements for the app to start.  :func:`_verify_required_env` walks
    this list at startup and raises ``RuntimeError`` for any in-scope absent
    variable.  Historically these were scattered ``raise RuntimeError`` checks in
    the FastAPI lifespan; v25 unified them here so a pre-promotion gate can read
    the list from the candidate SHA before deploying it.

QUALIFICATION prerequisites (:data:`QUALIFICATION_ENV`)
    Variables that must be present for the release-qualification gate to run
    fully.  Their absence degrades specific gate checks (e.g. the scratch-DB
    migration-apply gate defers with an actionable verdict) but does NOT prevent
    the app from booting.  The manifest is machine-readable by pre-promotion
    gates and diagnostic tooling so missing variables surface before repeated
    opaque deferrals accumulate.

PRESENCE-ONLY HARD RULE: this module only ever calls ``bool(os.getenv(name))``.
It never reads, compares, logs, or persists the env VALUE. Value-validity
specialisation (e.g. "is the Fernet key parseable") stays at the call site that
owns it (``_verify_encryption_config``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.field_encryption import ENCRYPTION_KEY_ENV

# Recognised scopes. ``always`` requirements are enforced in every runtime;
# ``cloud_run`` requirements are enforced only when the process is running on
# Cloud Run (signalled by the platform-injected ``K_REVISION`` env var).
VALID_SCOPES = frozenset({"always", "cloud_run"})

# Cloud Run injects this variable into every revision; it is the canonical
# "am I serving in production on Cloud Run" signal, mirrored from
# ``app.main._verify_encryption_config``.
CLOUD_RUN_SIGNAL_ENV = "K_REVISION"


@dataclass(frozen=True)
class EnvRequirement:
    """One environment variable the app requires to boot.

    Attributes:
        name: The environment variable name.
        scope: When the variable is required — ``'always'`` or ``'cloud_run'``.
        presence_only: Whether the manifest only checks presence (never value).
            Always ``True`` for now; the walker enforces presence-only regardless.
        reason: Human-readable explanation of why the variable is required.
    """

    name: str
    scope: str
    presence_only: bool = True
    reason: str = ""


# Single source of truth for env vars required to boot. Entries live here in the
# candidate repo (like ``backend/testing/smoketest.yaml``) so a pre-promotion
# gate can read them from the candidate SHA before deploying it.
REQUIRED_RUNTIME_ENV: list[EnvRequirement] = [
    EnvRequirement(
        name=ENCRYPTION_KEY_ENV,
        scope="cloud_run",
        reason=(
            "Field-level encryption at rest requires a Fernet key on Cloud Run. "
            "app.main._verify_encryption_config raises RuntimeError at startup "
            "when this is absent in production, to avoid silently storing "
            "credentials in plaintext."
        ),
    ),
]

# Env var name mirrored from migration_apply_gate to avoid a heavy import.
_MIGRATION_APPLY_SCRATCH_ENV = "MIGRATION_APPLY_SCRATCH_DATABASE_URL"

# Variables required for full release qualification but not for app boot.
# Absent variables degrade specific gate checks rather than preventing startup.
# Machine-readable so pre-promotion gates and diagnostic tooling can surface
# missing vars before opaque per-attempt deferrals accumulate.
QUALIFICATION_ENV: list[EnvRequirement] = [
    EnvRequirement(
        name=_MIGRATION_APPLY_SCRATCH_ENV,
        scope="cloud_run",
        reason=(
            "Pre-promotion scratch-DB migration-apply gate requires a dedicated "
            "PostgreSQL target whose user can CREATE and DROP databases.  Without "
            "this, every release candidate defers indefinitely with "
            "migration_apply_check_unavailable — a silent hold that only surfaces "
            "after repeated failed qualification attempts.  Bind a scratch-capable "
            "PostgreSQL URL (distinct from the production pool) to unblock "
            "qualification."
        ),
    ),
]


def missing_qualification_env() -> list[EnvRequirement]:
    """Return qualification prerequisites that are absent in the current runtime.

    Non-fatal: missing qualification vars degrade specific gate checks (they do
    NOT raise).  Call sites may log a warning or surface the list in diagnostics
    to help operators discover configuration gaps before qualification is
    attempted.
    """
    scopes = active_scopes()
    return [
        req
        for req in QUALIFICATION_ENV
        if req.scope in scopes and not bool(os.getenv(req.name))
    ]


def active_scopes() -> frozenset[str]:
    """Return the set of scopes in force for the current runtime."""
    scopes = {"always"}
    if bool(os.getenv(CLOUD_RUN_SIGNAL_ENV)):
        scopes.add("cloud_run")
    return frozenset(scopes)


def _verify_required_env() -> None:
    """Fail fast when any in-scope required env var is absent.

    Walks :data:`REQUIRED_RUNTIME_ENV`, filters to entries whose ``scope`` is in
    force for the current runtime, and raises ``RuntimeError`` naming every such
    entry whose variable is not set (``bool(os.getenv(name))`` is ``False``).

    Presence only: this never inspects the variable's value.
    """
    scopes = active_scopes()
    missing = [
        req
        for req in REQUIRED_RUNTIME_ENV
        if req.scope in scopes and not bool(os.getenv(req.name))
    ]
    if not missing:
        return

    lines = [
        f"  - {req.name} (scope={req.scope}): {req.reason}".rstrip()
        for req in missing
    ]
    raise RuntimeError(
        "[STARTUP] Required environment variable(s) missing for this runtime:\n"
        + "\n".join(lines)
    )
