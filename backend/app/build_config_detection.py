"""Shared helpers for build-config detection and operator-override preservation.

`_detect_build_config` (in ``app.routers.admin_onboard``) inspects a GitHub
repo and returns a freshly detected, BuildConfig-shaped dict. Several call
sites re-run detection on an *existing* project and must preserve the
operator-owned fields that are not derivable from repo contents (Git identity,
reviewer/parallelism toggles, branch namespace/prefixes, protected/stale branch
lists, self-improvement flag). This module houses that merge so the logic lives
in exactly one place and stays free of any admin_onboard <-> admin_projects
import cycle.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def apply_operator_build_config_overrides(
    detected: Dict[str, Any], existing: Dict[str, Any]
) -> Dict[str, Any]:
    """Re-apply operator-owned overrides from ``existing`` onto ``detected``.

    Mutates and returns ``detected``. Repo detection cannot recover these
    fields, so they are carried forward from the stored config:

    - ``git_user_email`` / ``git_user_name`` — preserved only when truthy.
    - ``env_vars``, reviewer/parallelism toggles, branch namespace/prefixes,
      ``pr_title_prefix``, staging/stale branch prefix lists,
      ``protected_branches`` and ``enable_self_improvement`` — preserved
      whenever the key is present in ``existing`` (including empty-list/falsey
      values), so deliberate operator customization survives re-detection.
    """
    existing = existing or {}

    if existing.get("git_user_email"):
        detected["git_user_email"] = existing["git_user_email"]
    if existing.get("git_user_name"):
        detected["git_user_name"] = existing["git_user_name"]
    if "env_vars" in existing:
        detected["env_vars"] = existing["env_vars"]
    if "enable_fresh_reviewer" in existing:
        detected["enable_fresh_reviewer"] = existing["enable_fresh_reviewer"]
    if "enable_parallel_review" in existing:
        detected["enable_parallel_review"] = existing["enable_parallel_review"]
    if "enable_parallel_fix" in existing:
        detected["enable_parallel_fix"] = existing["enable_parallel_fix"]
    # Per-project branch/PR naming overrides are user-set and not detectable
    # from repo contents; preserve them across re-detect so operators do not
    # lose deliberate customization.
    if "task_branch_namespace" in existing:
        detected["task_branch_namespace"] = existing["task_branch_namespace"]
    if "task_branch_prefix" in existing:
        detected["task_branch_prefix"] = existing["task_branch_prefix"]
    if "pr_title_prefix" in existing:
        detected["pr_title_prefix"] = existing["pr_title_prefix"]
    if "staging_branch_prefixes" in existing:
        detected["staging_branch_prefixes"] = existing["staging_branch_prefixes"]
    if "stale_branch_prefixes" in existing:
        detected["stale_branch_prefixes"] = existing["stale_branch_prefixes"]
    if "protected_branches" in existing:
        detected["protected_branches"] = existing["protected_branches"]
    if "enable_self_improvement" in existing:
        detected["enable_self_improvement"] = existing["enable_self_improvement"]

    return detected


async def maybe_refresh_empty_build_config(project, session, *, ref=None) -> bool:
    """Auto-refresh a project's ``build_config`` once its repo gains structure.

    A project onboarded against an empty/near-empty repository can store
    ``build_config.repo_is_empty=True`` even after ``directories`` were seeded
    by a manifest. As that repo is scaffolded over later PRs (real source files
    land on the integration branch), the stored signal must be re-evaluated and
    flipped to ``False`` once the source-file threshold is exceeded so the
    durable onboarding signal stops advertising a greenfield repo.

    This helper is the idempotent, cheap auto-refresh wired into integration
    landing paths:

    - The durable ``repo_is_empty`` guard makes it run only while the stored
      signal is still True. Once a refresh flips it False, later landing paths
      return immediately with NO GitHub call.
    - It reuses the EXISTING repo detector (``_detect_build_config``) against
      the project's *integration* branch and the same operator-override
      preservation the manual re-detect endpoint uses, so no second detector is
      introduced and operator-set fields survive.
    - It persists only when detection returns a valid BuildConfig; if the
      refreshed detection still classifies the repo as empty, the stored signal
      remains True and the helper stays best-effort.
    - It is best-effort: any exception is logged and swallowed so it can NEVER
      raise into or block a merge/landing path.

    Returns True iff the config was refreshed and persisted, else False.
    """
    try:
        existing = project.build_config or {}
        # Only refresh while the durable onboarding signal still says the repo
        # is empty/near-empty. Once a refresh flips repo_is_empty=False, the
        # helper becomes a no-op on later landing paths.
        if not existing.get("repo_is_empty"):
            return False
        if not getattr(project, "github_owner", None) or not getattr(
            project, "github_repo", None
        ):
            return False

        from pydantic import ValidationError
        from sqlalchemy.orm.attributes import flag_modified

        from app.github import get_repo_installation_token
        from app.routers.admin_onboard import _detect_build_config
        from app.schemas import BuildConfig as BuildConfigSchema
        from app.shared_state import get_project_integration_branch

        integration_branch = ref or get_project_integration_branch(project)

        token = await get_repo_installation_token(
            project.github_owner, project.github_repo
        )
        if not token:
            return False

        detected = await _detect_build_config(
            token,
            project.github_owner,
            project.github_repo,
            ref=integration_branch,
        )
        if not detected:
            return False

        detected = apply_operator_build_config_overrides(detected, existing)

        try:
            validated = BuildConfigSchema(**detected)
            project.build_config = validated.model_dump(exclude_none=True)
        except ValidationError:
            # Detection produced a shape the schema rejects — store raw so the
            # operator can fix it in the UI (mirrors the manual re-detect path).
            project.build_config = detected
        flag_modified(project, "build_config")
        await session.commit()
        logger.info(
            "[BUILD_CONFIG] Auto-refreshed empty build_config for project %s "
            "(integration branch %s) after repo gained structure",
            getattr(project, "id", None),
            integration_branch,
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort, must never break landing
        logger.warning(
            "[BUILD_CONFIG] Auto-refresh of empty build_config failed for "
            "project %s; leaving config unchanged",
            getattr(project, "id", None),
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 — keep the caller's session usable
            pass
        return False
