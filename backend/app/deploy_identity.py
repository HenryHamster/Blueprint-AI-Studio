"""Deploy-level identity — which GitHub repo the running deployment serves.

Post-promotion serving confirmation has two distinct observability classes:

  * **Merge-level** observability is available for EVERY GitHub project: the
    promotion target branch is reachable through the project's own GitHub
    credentials, so "did the promoted SHA persist on the target branch?" can
    always be answered (see ``release_artifacts.promotion_merge``).

  * **Deploy-level** observability — reading the live Cloud Run revision the
    orchestrator is itself running (``DeploymentState`` singleton, the
    ``DEPLOY_SHA`` resolver, the agent fleet's served source SHA) — is only
    meaningful for the ONE project whose repo this deployment was built from.
    Historically that was gated on the per-project ``is_self_repo`` boolean,
    but observability is not a property of the project row — it is a property
    of which repo THIS deployment serves.

This module resolves that deploy-level identity as an ``(owner, repo)`` pair
and exposes a predicate comparing a project against it.  The identity is read
from the ``DEPLOY_REPO_IDENTITY`` environment variable first (stamped on the
Cloud Run revision alongside ``DEPLOY_SHA``), then from the
``deploy_repo_identity`` runtime-config key (operator-settable via
``PATCH /api/admin/settings`` with no redeploy).  When neither is set the
deployment has no self-identity and the deploy-level providers are observable
for no project — every promotion still gets the merge-level verdict.

Comparison is case-insensitive and ``owner``/``repo`` are matched independently,
mirroring how GitHub treats repository slugs.
"""

from __future__ import annotations

import os

DeployIdentity = tuple[str, str] | None


def _parse_owner_repo(raw: object) -> DeployIdentity:
    """Parse an ``"owner/repo"`` string into a lowercased ``(owner, repo)`` pair.

    Returns ``None`` for anything that is not a non-empty ``owner/repo`` string
    (missing value, wrong type, no slash, empty component) so a malformed
    identity degrades to "no deploy identity" rather than matching spuriously.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if "/" not in value:
        return None
    owner, _, repo = value.partition("/")
    owner = owner.strip().lower()
    repo = repo.strip().lower()
    if not owner or not repo:
        return None
    return (owner, repo)


def deploy_repo_identity() -> DeployIdentity:
    """Resolve the ``(owner, repo)`` this deployment serves, or ``None``.

    Resolution order (first hit wins):
      1. ``DEPLOY_REPO_IDENTITY`` env var (``"owner/repo"``) — stamped on the
         Cloud Run revision by the deploy pipeline.
      2. ``runtime_config['deploy_repo_identity']`` — operator override, live
         via ``PATCH /api/admin/settings``.

    Never raises; an unset/malformed source yields ``None``.
    """
    env_identity = _parse_owner_repo(os.getenv("DEPLOY_REPO_IDENTITY"))
    if env_identity is not None:
        return env_identity

    # Lazy import keeps this module free of a load-time dependency on the
    # runtime-config singleton (which imports a wide surface at module load).
    from app.runtime_config_state import runtime_config

    return _parse_owner_repo(runtime_config.get("deploy_repo_identity"))


def project_matches_deploy_identity(
    project: object,
    identity: DeployIdentity = None,
) -> bool:
    """True iff *project*'s GitHub repo IS the repo this deployment serves.

    ``identity`` may be passed in (resolve it once, reuse across many projects)
    or left ``None`` to resolve it here.  Returns ``False`` when there is no
    deploy identity, the project is missing, or the project has no GitHub repo —
    so a deploy-level provider is observable for at most one project and never
    for a credential-less / mismatched one.

    Deliberately does NOT read ``project.is_self_repo`` — deploy-level
    observability is determined solely by repo identity, which deprecates that
    column for this purpose.
    """
    if identity is None:
        identity = deploy_repo_identity()
    if identity is None or project is None:
        return False
    owner, repo = identity
    project_owner = (getattr(project, "github_owner", None) or "").strip().lower()
    project_repo = (getattr(project, "github_repo", None) or "").strip().lower()
    if not project_owner or not project_repo:
        return False
    return project_owner == owner and project_repo == repo
