"""Task-branch naming + per-project branch/namespace resolution (split out of shared_state).

Owns the agent work-branch derivation, the per-project integration / promotion /
namespace / PR-title-prefix resolvers, and the two startup backfills that
populate the corresponding columns. Re-exported from :mod:`app.shared_state` as
a thin facade so existing importers are unaffected. Behaviour-preserving.
"""

import logging
from typing import Any, Dict

from sqlalchemy.orm import object_session
from sqlalchemy.orm.attributes import flag_modified

from app.runtime_config_state import runtime_config

logger = logging.getLogger(__name__)

# Canonical default for task scope when not explicitly set.
# Operational modules (orchestrator, review pipeline, research completion) use this.
# Analytics/tracking modules use "unknown" to distinguish missing data from a real scope.
DEFAULT_TASK_SCOPE = "small"

# Maps calibrated task scope to a baseline effort level used at spawn time.
# small work runs faster/cheaper; large work gets deeper decomposition/analysis.
# Used by research_completion and task_completion as a floor on top of
# finding/parent-specified effort via _effort_rank (finding wins when higher).
_SCOPE_EFFORT_MAP = {"small": "low", "medium": "medium", "large": "high"}


def scope_to_effort(scope: str | None) -> str:
    """Return the baseline effort for a calibrated scope.

    Unknown / missing scopes fall back to "medium" so callers can safely take
    the max against other effort signals without suppressing them.
    """
    return _SCOPE_EFFORT_MAP.get(scope or "", "medium")


# Maps a task type to its branch prefix. Must stay in sync with the agent's
# branch derivation (agent/src/index.ts, handlers/specialized.ts). When an
# orchestrator path needs to address an agent-pushed branch (review/fix spawn,
# operator merge), it must derive the name with this map — hardcoding
# "blueprint/task-{8hex}" silently broke review spawns for non-default task
# types (refactor → blueprint/refactor-{8hex}, etc.); see 2026-05-25 incident
# where PRs #5779/#5812/#5815/#5819 all stalled at review cap because the
# clone targeted blueprint/task-* against agents that had pushed
# blueprint/refactor-*.
TASK_TYPE_BRANCH_PREFIX: Dict[str, str] = {
    "create": "task",
    "qa": "task",
    "fix": "task",
    "refactor": "refactor",
    "docs": "docs",
    "deps": "deps",
    "migrate": "migrate",
}


# Canonical namespace for the Blueprint self-repo (the "blueprint" segment of
# ``blueprint/{prefix}-{8hex}``).  This is NOT a silent default for
# ``derive_task_branch`` — callers must pass the project's resolved namespace
# explicitly (see :func:`get_project_task_branch_namespace`).  It survives only
# as the terminal fallback inside ``get_project_task_branch_namespace`` and as
# the value the namespace backfill writes for Blueprint self-repo projects.
TASK_BRANCH_NAMESPACE_DEFAULT = "blueprint"


_missing_project_branch_alerts: set[tuple[str, str]] = set()


def derive_task_branch(
    task_id: Any,
    task_type: str | None,
    namespace: str,
) -> str:
    """Derive an agent work branch name (``{namespace}/{prefix}-{8hex}``)
    from the task type and id, matching the agent's branch derivation.

    ``namespace`` is REQUIRED — there is no silent ``"blueprint"`` default, so
    every call site for an arbitrary repo must thread through the project's
    actual namespace (resolve it via
    :func:`get_project_task_branch_namespace`).  Passing the wrong namespace
    yields a branch name no agent ever pushed and strands downstream review /
    fix respawns at ``git clone --branch``.
    """
    prefix = TASK_TYPE_BRANCH_PREFIX.get(task_type or "", "task")
    return f"{namespace}/{prefix}-{str(task_id).replace('-', '')[:8]}"


def _signal_missing_project_branch(project, column_name: str, alert_title: str) -> None:
    project_id = getattr(project, "id", None)
    project_id_str = str(project_id) if project_id is not None else "<unknown>"
    dedupe_key = (project_id_str, column_name)
    if dedupe_key in _missing_project_branch_alerts:
        return
    _missing_project_branch_alerts.add(dedupe_key)
    logger.warning(
        "[BRANCH_RESOLVER] project %s missing %s; falling back to main",
        project_id_str,
        column_name,
    )

    try:
        session = object_session(project)
    except Exception:  # noqa: BLE001
        session = None
    if session is None or project_id is None:
        return

    from app.models import Alert, AlertSeverity

    try:
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.ERROR,
                title=alert_title,
                message=(
                    f"Project {project_id_str} has NULL {column_name}; "
                    "falling back to main until the column is populated."
                ),
            )
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[BRANCH_RESOLVER] project %s missing %s; alert could not be staged",
            project_id_str,
            column_name,
        )


def resolve_task_branch(task: Any, namespace: str) -> str:
    """Return the branch a task's agent actually pushed to.

    Precedence:
      1. ``task.context["review_loop"]["branch"]`` — set by review/fix spawn
      2. ``task.first_payload["branch"]`` — set when an agent claims a task
      3. ``task.context["branch"]`` — legacy fallback
      4. ``derive_task_branch(task.id, task_type, namespace)`` — last-resort
         synthesis

    ``namespace`` is REQUIRED and is used only for the last-resort synthesis
    branch (4); callers should resolve it from the task's project via
    :func:`get_project_task_branch_namespace`.  It no longer silently defaults
    to ``"blueprint"`` so a non-Blueprint repo never synthesizes a branch under
    the wrong namespace.

    Fix tasks reuse the parent's PR branch — synthesizing
    ``{namespace}/task-{fix_id}`` from a fix child's own id yields a name no
    agent ever pushed, breaking downstream review respawns that try to clone
    it. The first three sources carry the real branch; derivation is only
    safe when none of them are present (a freshly-spawned create/refactor
    task with no payload-provided branch yet).
    """
    ctx = getattr(task, "context", None) or {}
    review_loop = ctx.get("review_loop") or {}
    branch = review_loop.get("branch")
    if branch:
        return branch
    payload = getattr(task, "first_payload", None) or {}
    branch = payload.get("branch")
    if branch:
        return branch
    branch = ctx.get("branch")
    if branch:
        return branch
    return derive_task_branch(getattr(task, "id", None), payload.get("type"), namespace)


def get_project_integration_branch(project) -> str:
    """Return the integration branch for a project.

    Fallback chain:
      1. project.integration_branch (per-project column, added by migration 0122+)
      2. "automain" for self-repo projects (qualification trunk)
      3. "main" for non-self-repo projects (promotion/release target)

    The deprecated runtime_config bridge was removed after the per-project
    column stabilized across the migration/backfill rollout.
    """
    integration_branch = getattr(project, "integration_branch", None)
    if integration_branch:
        return integration_branch
    _signal_missing_project_branch(
        project, "integration_branch", "Missing integration_branch"
    )
    if _is_blueprint_self_repo(project):
        return "automain"
    return "main"


def get_project_task_branch_namespace(project) -> str:
    """Return the task-branch namespace for a project.

    Fallback chain:
      1. project.build_config['task_branch_namespace'] (per-project override)
      2. runtime_config['task_branch_namespace'] (DEPRECATED global fallback)
      3. ``TASK_BRANCH_NAMESPACE_DEFAULT`` (``"blueprint"``)

    A non-Blueprint install (e.g. a project named ``autoproj``) configures its
    namespace through ``build_config`` so its agent work branches land under
    ``autoproj/{prefix}-{8hex}`` instead of the Blueprint default.
    """
    if project is not None:
        build_config = getattr(project, "build_config", None) or {}
        per_project = build_config.get("task_branch_namespace")
        if per_project:
            return str(per_project)
    return runtime_config.get("task_branch_namespace", TASK_BRANCH_NAMESPACE_DEFAULT)


def get_project_pr_title_prefix(project) -> str:
    """Return the PR title prefix string for a project, with a trailing space.

    Reads ``build_config['pr_title_prefix']``.  If set, returns the value with
    a single trailing space so callers can write ``f"{prefix}Rollup: ..."``
    directly.  If absent or empty, returns ``""`` so no prefix is injected.

    Example ``build_config`` entry::

        {"pr_title_prefix": "[Blueprint]"}
    """
    if project is not None:
        build_config = getattr(project, "build_config", None) or {}
        per_project = build_config.get("pr_title_prefix")
        if per_project:
            return f"{per_project} "
    return ""


def ensure_project_pr_title_prefix(project, title: str) -> str:
    """Return *title* with the project's PR title prefix prepended if absent.

    Used by the backend PR-creation endpoint (``/api/agent/github/pull-requests``)
    to normalise agent-authored PR titles so they carry the project's configured
    branding prefix even when the agent's prompt did not include it explicitly.

    Idempotent: if *title* already starts with the prefix token (ignoring the
    trailing space that :func:`get_project_pr_title_prefix` appends), returns
    *title* unchanged.  When no prefix is configured returns *title* as-is.
    """
    prefix = get_project_pr_title_prefix(project)
    if not prefix:
        return title
    # prefix already carries a trailing space (e.g. "[Blueprint] ").
    # Compare against the bare token so "[Blueprint] feat: x" is not re-prefixed.
    if title.startswith(prefix.rstrip()):
        return title
    return f"{prefix}{title}"


def get_project_promotion_target(project) -> str:
    """Return the promotion target branch for a project.

    Fallback chain:
      1. project.promotion_target (per-project column, added by migration 0125+)
      2. "main"

    The deprecated runtime_config bridge was removed after the per-project
    column stabilized across the migration/backfill rollout.
    """
    promotion_target = getattr(project, "promotion_target", None)
    if promotion_target:
        return promotion_target
    _signal_missing_project_branch(
        project, "promotion_target", "Missing promotion_target"
    )
    return "main"


async def backfill_branch_columns(session, project_id=None) -> dict:
    """Populate NULL integration_branch / promotion_target from GitHub defaults.

    Runs once at startup (called from main.py lifespan) after migrations finish.
    Only writes to columns that are still NULL — skips rows already populated so
    the function is safe to re-run.  Self-repo projects with NULL integration_branch
    are left untouched (migration 0124 already set them to 'automain'; if still NULL
    they are in a state the LTS workflow manages separately).  Self-repo projects
    with NULL promotion_target get 'main' without a GitHub round-trip.

    For non-self-repo projects, resolves GitHub credentials and calls
    get_repo_default_branch().  When the GitHub API call fails, emits a per-project
    Alert so an operator can fix credentials and re-run rather than silently falling
    back to 'main'.

    When project_id is provided, only that project is processed.  Used by
    POST /api/admin/projects/{id}/backfill-branches to re-run the backfill for a
    single project without restarting the service.

    Returns a summary dict:
      {
        "filled": int,     # columns written
        "skipped": int,    # projects already fully populated
        "errored": int,    # projects where GitHub API call failed
        "self_repo_skipped": int,  # self-repo integration_branch left as-is
      }
    """
    from app.models import Alert, AlertSeverity, Project
    from app.release_promotion import _resolve_github_with_session
    from app import github as _github
    from sqlmodel import select

    summary = {"filled": 0, "skipped": 0, "errored": 0, "self_repo_skipped": 0}

    try:
        query = select(Project).where(
            (Project.integration_branch.is_(None))
            | (Project.promotion_target.is_(None))
        )
        if project_id is not None:
            query = query.where(Project.id == project_id)
        result = await session.execute(query)
        projects = result.scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BRANCH_BACKFILL] Could not query projects: %s", exc)
        return summary

    for project in projects:
        need_integration = project.integration_branch is None
        need_promotion = project.promotion_target is None

        if not need_integration and not need_promotion:
            summary["skipped"] += 1
            continue

        # Self-repo: integration_branch managed by the LTS workflow — leave NULL
        # as-is (migration 0124 already backfilled is_self_repo rows to 'automain').
        if project.is_self_repo and need_integration:
            summary["self_repo_skipped"] += 1
            need_integration = False

        # Self-repo promotion_target: no GitHub round-trip needed.
        if project.is_self_repo and need_promotion:
            project.promotion_target = "main"
            flag_modified(project, "promotion_target")
            session.add(project)
            summary["filled"] += 1
            need_promotion = False

        if not need_integration and not need_promotion:
            continue

        # Non-self-repo: resolve GitHub credentials and fetch the default branch.
        try:
            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                raise ValueError("no GitHub credentials available")
            token, owner, repo = resolved
            default_branch = await _github.get_repo_default_branch(token, owner, repo)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[BRANCH_BACKFILL] project %s (%s/%s): GitHub call failed: %s",
                project.id,
                getattr(project, "github_owner", "?"),
                getattr(project, "github_repo", "?"),
                exc,
            )
            try:
                alert = Alert(
                    project_id=project.id,
                    severity=AlertSeverity.WARNING,
                    title="Branch backfill failed — integration_branch or promotion_target still NULL",
                    message=(
                        f"Could not populate integration_branch/promotion_target for project "
                        f"{project.id} from GitHub: {str(exc)[:300]}. "
                        "Fix GitHub credentials and re-run the backfill (restart the service or "
                        "call POST /api/admin/projects/{id}/backfill-branches)."
                    ),
                )
                session.add(alert)
            except Exception as alert_exc:  # noqa: BLE001
                logger.debug(
                    "[BRANCH_BACKFILL] alert insertion failed (non-fatal): %s",
                    alert_exc,
                )
            summary["errored"] += 1
            continue

        if need_integration:
            project.integration_branch = default_branch
            flag_modified(project, "integration_branch")
            summary["filled"] += 1
        if need_promotion:
            project.promotion_target = default_branch
            flag_modified(project, "promotion_target")
            summary["filled"] += 1
        session.add(project)

    if summary["filled"] or summary["errored"]:
        logger.info(
            "[BRANCH_BACKFILL] complete: filled=%d skipped=%d errored=%d self_repo_skipped=%d",
            summary["filled"],
            summary["skipped"],
            summary["errored"],
            summary["self_repo_skipped"],
        )

    return summary


# Historical Blueprint repo aliases whose canonical task-branch namespace is
# "blueprint".  Slug-derivation of "BlueprintAIStudio" would otherwise produce
# "blueprintaistudio" — divergent from what the agent's hardcoded push path
# uses, which strands every merge-conflict fix spawn (incident 2026-05-26..28,
# six failed fix tasks before the wedge below landed).
_BLUEPRINT_REPO_ALIASES = frozenset({"blueprintaistudio"})


def _is_blueprint_self_repo(project) -> bool:
    """True when *project* should use the canonical ``"blueprint"`` namespace.

    Catches both the correctly-flagged case (``is_self_repo=True``) and the
    historically-broken case where ``is_self_repo`` is misset False but the
    GitHub repo name is a known Blueprint alias.
    """
    if getattr(project, "is_self_repo", False):
        return True
    github_repo = getattr(project, "github_repo", "") or ""
    return github_repo.lower() in _BLUEPRINT_REPO_ALIASES


async def backfill_task_branch_namespace(session, project_id=None) -> dict:
    """Populate or correct ``task_branch_namespace`` in ``build_config``.

    Runs at startup and via the admin backfill endpoint.

    Two paths:
      * **Fill-when-empty (legacy):** if the namespace is absent or empty, set
        it to ``"blueprint"`` for Blueprint self-repo projects and to a
        slugified ``github_repo`` for everything else.  Mirrors
        ``_derive_task_branch_namespace`` in ``admin_onboard.py``.
      * **Corrective overwrite (incident 2026-05-26..28):** if the project is
        the Blueprint self-repo (by flag or by historical alias —
        ``BlueprintAIStudio``) but the persisted namespace differs from
        ``"blueprint"``, force-overwrite it.  The agent always pushes to
        ``blueprint/...``; an out-of-sync orchestrator-side namespace makes
        every merge-conflict fix-spawn try to clone a nonexistent branch.
        Six failed fix tasks tripped this in ~36h before the wedge landed.

    Idempotent for projects already at the expected value.  When *project_id*
    is provided only that project is processed.

    Returns a summary dict::

        {
          "namespace_filled":    int,  # missing value populated
          "namespace_skipped":   int,  # already correct, untouched
          "namespace_corrected": int,  # stale value overwritten
        }
    """
    import re as _re

    from app.models import Project
    from sqlmodel import select

    summary = {
        "namespace_filled": 0,
        "namespace_skipped": 0,
        "namespace_corrected": 0,
    }

    try:
        query = select(Project)
        if project_id is not None:
            query = query.where(Project.id == project_id)
        result = await session.execute(query)
        projects = result.scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NS_BACKFILL] Could not query projects: %s", exc)
        return summary

    for project in projects:
        build_config = project.build_config or {}
        current_ns = build_config.get("task_branch_namespace") or ""
        github_repo = getattr(project, "github_repo", "") or ""
        is_blueprint = _is_blueprint_self_repo(project)

        if is_blueprint:
            expected_ns = "blueprint"
            if current_ns == expected_ns:
                summary["namespace_skipped"] += 1
                continue
            if current_ns:
                logger.warning(
                    "[NS_BACKFILL] project %s (%s): correcting stale namespace %r -> %r",
                    project.id,
                    github_repo or "<no-repo>",
                    current_ns,
                    expected_ns,
                )
                summary["namespace_corrected"] += 1
            else:
                summary["namespace_filled"] += 1
            project.build_config = {
                **build_config,
                "task_branch_namespace": expected_ns,
            }
            flag_modified(project, "build_config")
            session.add(project)
            continue

        # Non-Blueprint project: legacy fill-when-empty path remains idempotent.
        if current_ns:
            summary["namespace_skipped"] += 1
            continue

        slug = _re.sub(r"[^a-z0-9]+", "-", github_repo.lower()).strip("-")
        namespace = slug[:40] if slug else "project"
        project.build_config = {**build_config, "task_branch_namespace": namespace}
        flag_modified(project, "build_config")
        session.add(project)
        summary["namespace_filled"] += 1

    if summary["namespace_filled"] or summary["namespace_corrected"]:
        logger.info(
            "[NS_BACKFILL] complete: namespace_filled=%d namespace_skipped=%d namespace_corrected=%d",
            summary["namespace_filled"],
            summary["namespace_skipped"],
            summary["namespace_corrected"],
        )

    return summary


def get_project_broadcast_limit(project) -> int:
    """Return the broadcast alert limit for a project.

    Fallback chain:
      1. project.design_doc['_broadcast_limit'] (per-project override via admin endpoint)
      2. runtime_config["project_broadcast_limit"] (global default)
      3. 1000
    """
    per_project = (getattr(project, "design_doc", None) or {}).get("_broadcast_limit")
    if per_project is not None:
        return int(per_project)
    return runtime_config.get("project_broadcast_limit", 1000)
