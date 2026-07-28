"""Declarative manifest of environment variables the app requires to boot.

BOOT contract (:data:`REQUIRED_RUNTIME_ENV`)
    Hard requirements for the app to start.  :func:`_verify_required_env` walks
    this list at startup and raises ``RuntimeError`` for any in-scope absent
    variable.  Historically these were scattered ``raise RuntimeError`` checks in
    the FastAPI lifespan; v25 unified them here so a pre-promotion gate can read
    the list from the candidate SHA before deploying it.

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
# Cloud Run (signalled by the platform-injected ``K_REVISION`` env var), while
# ``local_production`` is an explicit operator-selected scope for a local
# control plane. Do not infer local production from host names or a missing
# Cloud Run marker: that would turn development/test processes into deployments.
VALID_SCOPES = frozenset({"always", "cloud_run", "local_production"})

# Cloud Run injects this variable into every revision; it is the canonical
# "am I serving in production on Cloud Run" signal, mirrored from
# ``app.main._verify_encryption_config``.
CLOUD_RUN_SIGNAL_ENV = "K_REVISION"
LOCAL_DEPLOYMENT_MODE_ENV = "BLUEPRINT_DEPLOYMENT_MODE"
LOCAL_PRODUCTION_DEPLOYMENT_MODE = "local-production"


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
    EnvRequirement(
        name=ENCRYPTION_KEY_ENV,
        scope="local_production",
        reason=(
            "A local production control plane handles the same credentials as "
            "Cloud Run and requires a Fernet key; plaintext credentials are "
            "never permitted in this deployment mode."
        ),
    ),
]

def active_scopes() -> frozenset[str]:
    """Return the set of scopes in force for the current runtime."""
    scopes = {"always"}
    if bool(os.getenv(CLOUD_RUN_SIGNAL_ENV)):
        scopes.add("cloud_run")
    if (
        os.getenv(LOCAL_DEPLOYMENT_MODE_ENV, "").strip().lower()
        == LOCAL_PRODUCTION_DEPLOYMENT_MODE
    ):
        scopes.add("local_production")
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
