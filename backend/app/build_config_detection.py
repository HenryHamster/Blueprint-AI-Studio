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
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _normalize_dir_path(path: Any) -> str:
    """Normalize a directory path for matching across config sources.

    Recovered directories carry repo-root-relative paths (``"."``, ``"services/api"``)
    that must match the stored ``build_config.directories[*].path`` regardless of a
    leading ``"./"`` or trailing slash. An empty/whitespace path collapses to ``"."``
    (the repo root), matching the seeding convention.
    """
    s = str(path or "").strip()
    if s.startswith("./"):
        s = s[2:]
    s = s.rstrip("/")
    return s or "."


def apply_operator_build_config_overrides(
    detected: Dict[str, Any], existing: Dict[str, Any]
) -> Dict[str, Any]:
    """Re-apply operator-owned overrides from ``existing`` onto ``detected``.

    Mutates and returns ``detected``. Repo detection cannot recover these
    fields, so they are carried forward from the stored config:

    - ``git_user_email`` / ``git_user_name`` — preserved only when truthy.
    - ``env_vars``, reviewer/parallelism toggles, branch namespace/prefixes,
      ``pr_title_prefix``, staging/stale branch prefix lists,
      ``protected_branches``, ``enable_self_improvement``,
      ``requires_ci_checks`` and the per-project parallelism caps
      (``max_parallel_research``, ``max_total_research``,
      ``max_autonomous_tasks_per_hour``) — preserved whenever the key is
      present in ``existing`` (including empty-list/falsey values), so
      deliberate operator customization survives re-detection.
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
    # The no-CI merge policy is operator-owned and never emitted by repo
    # detection; carry an explicit choice (including False) forward so manual
    # re-detect and empty-config auto-refresh do not silently re-gate the repo.
    # Safety clamp: if the freshly detected config has has_ci=False (the repo
    # has zero CI workflows and can never produce check-runs), override a stale
    # requires_ci_checks=True from the existing config — that combination is an
    # impossible-to-satisfy state that would strand main-targeting rollup PRs.
    if "requires_ci_checks" in existing:
        if detected.get("has_ci") is False and existing.get("requires_ci_checks"):
            detected["requires_ci_checks"] = False
        else:
            detected["requires_ci_checks"] = existing["requires_ci_checks"]
    # Per-project parallelism overrides are operator-owned and never emitted
    # by repo detection; carry them forward so re-detect does not silently
    # lift a deliberate throttle (see schemas.BuildConfig).
    for _cap_key in (
        "max_parallel_research",
        "max_total_research",
        "max_autonomous_tasks_per_hour",
    ):
        if _cap_key in existing:
            detected[_cap_key] = existing[_cap_key]
    # Per-project resource envelope (concurrent-VM caps and spend ceilings) is
    # operator-owned and never emitted by repo detection; carry it forward so
    # re-detect does not silently drop an operator-set envelope.
    if "resource_envelope" in existing:
        detected["resource_envelope"] = existing["resource_envelope"]
    # Repo-bootstrap command-memory status is operator-visible completion state,
    # not repo-derived config; carry it forward so refreshes do not erase the
    # durable outcome stamped by task completion.
    for _cm_key in (
        "bootstrap_command_memory_status",
        "bootstrap_command_memory_complete",
        "bootstrap_command_memory_detail",
        "bootstrap_command_memory_paths",
        "bootstrap_command_memory_updated_at",
    ):
        if _cm_key in existing:
            detected[_cm_key] = existing[_cm_key]
    # Onboarding-proof milestone timestamps are durable event records, not
    # repo-derived config; carry them forward so re-detect never erases evidence.
    for _op_key in (
        "first_task_spawned_at",
        "first_pr_opened_at",
        "first_merge_gate_verdict_at",
        "first_merge_gate_executed_tests",
        "first_merge_gate_unverified_reason",
        "first_pr_merged_at",
    ):
        if _op_key in existing:
            detected[_op_key] = existing[_op_key]

    # Merge per-directory agent-confirmed command fields back onto re-detected
    # directories.  These are written by the agent write-back endpoint and are
    # NOT derivable from repo contents, so they must survive re-detection.
    _CONFIRMED_DIR_KEYS = (
        "confirmed_lint_cmd",
        "confirmed_test_cmd",
        "confirmed_build_cmd",
        "confirmed_install_cmd",
        "commands_confirmed_at",
        "confirming_task_id",
    )
    existing_dirs = existing.get("directories") or []
    existing_by_path: Dict[str, Dict[str, Any]] = {}
    for _d in existing_dirs:
        if isinstance(_d, dict):
            _norm = _normalize_dir_path(_d.get("path"))
            existing_by_path[_norm] = _d
    if existing_by_path:
        detected_dirs = detected.get("directories") or []
        for _entry in detected_dirs:
            if not isinstance(_entry, dict):
                continue
            _norm = _normalize_dir_path(_entry.get("path"))
            _ex = existing_by_path.get(_norm)
            if _ex:
                for _k in _CONFIRMED_DIR_KEYS:
                    if _k in _ex and _ex[_k] is not None:
                        _entry[_k] = _ex[_k]

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
        # Refresh while EITHER durable onboarding signal still says this repo
        # warrants re-detection:
        #   - ``repo_is_empty``: a greenfield repo whose structure may have
        #     landed since onboarding, or
        #   - ``detection_incomplete``: onboarding-time detection never completed
        #     (a transient GitHub failure), so the stored config has no real test
        #     command and the no-CI gate would merge UNVERIFIED forever.
        # A repo whose detection COMPLETED and genuinely found no tests sets
        # neither flag, so it is never re-scanned here. Once a successful
        # re-detect persists a config (flipping repo_is_empty=False and dropping
        # the detection_incomplete marker), the helper becomes a no-op on later
        # landing paths.
        if not existing.get("repo_is_empty") and not existing.get(
            "detection_incomplete"
        ):
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


async def maybe_redetect_missing_test_command(project, session, *, ref=None) -> bool:
    """Best-effort re-detection for a non-empty, detection-complete project with no test command.

    When onboarding-time detection completed but found no test command for a
    non-empty external repo, the no-CI merge gate falls open to UNVERIFIED on
    every subsequent PR.  This helper attempts a single bounded re-detection
    using the same detector and operator-override-preserving path as
    :func:`maybe_refresh_empty_build_config`, but guards on the OPPOSITE
    condition: fires only when detection was NOT marked incomplete or empty (so
    it never competes with that path) and the project currently has no
    detectable test command.

    Returns True iff re-detection found a test command and persisted it.
    Best-effort: never raises into the caller; logs, rolls back the session on
    failure, and returns False.
    """
    try:
        existing = project.build_config or {}
        # Only fire for non-empty, detection-complete projects with no test cmd.
        # repo_is_empty / detection_incomplete are handled by
        # maybe_refresh_empty_build_config — do not compete with that path.
        if existing.get("repo_is_empty") or existing.get("detection_incomplete"):
            return False
        if not getattr(project, "github_owner", None) or not getattr(
            project, "github_repo", None
        ):
            return False

        from app import ci_resolution

        if ci_resolution.has_detectable_tests(project):
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

        # Only persist when re-detection actually found a test command.
        class _Stub:
            pass

        _stub = _Stub()
        _stub.build_config = detected
        if not ci_resolution.has_detectable_tests(_stub):
            return False

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
            "[BUILD_CONFIG] Re-detected test command for non-empty project %s "
            "(integration branch %s) — no-CI gate will now run tests",
            getattr(project, "id", None),
            integration_branch,
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort, must never block a merge path
        logger.warning(
            "[BUILD_CONFIG] Re-detection of missing test command failed for "
            "project %s; leaving config unchanged",
            getattr(project, "id", None),
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 — keep the caller's session usable
            pass
        return False


_MAX_DISCOVERED_ENTRIES = 50
_MAX_CMD_LENGTH = 500


async def persist_discovered_build_commands(
    project, session, discovered_directories: List[Dict[str, Any]]
) -> bool:
    """Persist agent-discovered build commands into ``build_config`` using FILL-ONLY semantics.

    When an agent VM discovers runnable test/lint/build/install commands during a task
    run (full repo clone, real test-runner detection), this helper folds those
    commands back into ``project.build_config.directories`` so future runs benefit
    from the agent's first-hand knowledge. It is the complement of
    :func:`persist_recovered_test_command` and generalises to all four command
    fields (lint_cmd, build_cmd, test_cmd, install_cmd).

    Merge semantics:

    - FILL-ONLY: only write a field when the stored entry does NOT already have a
      truthy value for it. An existing non-empty command is NEVER overwritten.
    - JUNK GUARDS: only entries flagged with ``executed_successfully=True`` are
      persisted; per-command strings are truncated to ``_MAX_CMD_LENGTH``; at most
      ``_MAX_DISCOVERED_ENTRIES`` entries are accepted; if ``build_config.directories``
      is already non-empty the report is ignored entirely (this path fills missing
      detection, not configured repos).
    - Clears ``detection_incomplete`` when at least one real command is persisted.
    - Validates the merged dict against BuildConfig before persisting; on
      ValidationError falls back to storing the raw dict (mirrors the existing
      raw-store fallback in :func:`maybe_refresh_empty_build_config`).
    - BEST-EFFORT: never raises into the caller; logs, rolls back the session on
      failure, and returns a bool indicating whether a change was persisted.
    """
    try:
        if not discovered_directories:
            return False

        from pydantic import ValidationError
        from sqlalchemy.orm.attributes import flag_modified

        from app.schemas import BuildConfig as BuildConfigSchema

        existing = dict(project.build_config or {})
        directories = [dict(d) for d in (existing.get("directories") or [])]

        # JUNK GUARD: ignore the report entirely when build_config already has
        # populated directories — this path is only for filling incomplete detection.
        if directories:
            return False

        index_by_path = {
            _normalize_dir_path(d.get("path")): i for i, d in enumerate(directories)
        }

        _CMD_FIELDS = ("lint_cmd", "build_cmd", "test_cmd", "install_cmd")
        changed = False

        for raw in discovered_directories[:_MAX_DISCOVERED_ENTRIES]:
            if not isinstance(raw, dict):
                continue
            # JUNK GUARD: only persist entries with explicit execution evidence.
            if not raw.get("executed_successfully"):
                continue

            norm = _normalize_dir_path(raw.get("path"))
            rec_type = raw.get("type")

            if norm in index_by_path:
                entry = directories[index_by_path[norm]]
                # FILL-ONLY for type
                if rec_type and not entry.get("type"):
                    entry["type"] = rec_type
                    changed = True
                # FILL-ONLY for each command field
                for field in _CMD_FIELDS:
                    raw_val = str(raw.get(field) or "").strip()
                    if raw_val and not entry.get(field):
                        entry[field] = raw_val[:_MAX_CMD_LENGTH]
                        changed = True
            else:
                new_entry: Dict[str, Any] = {"path": norm}
                if rec_type:
                    new_entry["type"] = rec_type
                for field in _CMD_FIELDS:
                    raw_val = str(raw.get(field) or "").strip()
                    if raw_val:
                        new_entry[field] = raw_val[:_MAX_CMD_LENGTH]
                has_any_cmd = any(new_entry.get(f) for f in _CMD_FIELDS)
                if has_any_cmd:
                    directories.append(new_entry)
                    index_by_path[norm] = len(directories) - 1
                    changed = True

        if not changed:
            return False

        existing["directories"] = directories
        # Clear detection_incomplete so the auto-refresh path stops re-running.
        existing.pop("detection_incomplete", None)
        existing["detection_incomplete"] = False

        try:
            validated = BuildConfigSchema(**existing)
            project.build_config = validated.model_dump(exclude_none=True)
        except ValidationError:
            # A discovered entry produced a shape the schema rejects — store raw
            # so the commands still take effect and the operator can fix in the UI.
            project.build_config = existing
        flag_modified(project, "build_config")
        await session.commit()
        logger.info(
            "[BUILD_CONFIG] Persisted agent-discovered build commands for "
            "project %s: %s dirs filled",
            getattr(project, "id", None),
            len(directories),
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort, must never fail the agent run
        logger.warning(
            "[BUILD_CONFIG] Persisting agent-discovered build commands failed for "
            "project %s; leaving config unchanged",
            getattr(project, "id", None),
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 — keep the caller's session usable
            pass
        return False


async def persist_recovered_test_command(
    project, session, recovered_directories: List[Dict[str, Any]]
) -> bool:
    """Persist a self-healed no-CI test command back into ``build_config``.

    When the no-CI merge gate's ``verify_tests`` run discovers that a project's
    stored test command is stale and runs a real replacement suite from the
    already-cloned workspace, it reports the per-directory configs that ACTUALLY
    RAN as ``recovered_directories`` (``[{path, type, test_cmd}, ...]``,
    repo-root-relative). This helper folds those corrected commands back into
    ``project.build_config.directories`` so FUTURE merges use the runnable
    command instead of re-discovering it on every poll.

    Merge semantics:

    - Match an existing directory by normalized ``path`` and update its
      ``test_cmd`` (and ``type`` only when the stored entry is missing one);
      append a new entry for a recovered directory with no stored match.
    - ONLY operates on the ``directories`` list — every operator-owned field
      (Git identity, branch prefixes, ``requires_ci_checks``, ``env_vars``,
      etc.) is left untouched, so operator customization is preserved.
    - CONSERVATIVE GUARD: a recovered entry is applied only when it carries a
      concrete, non-empty ``test_cmd``. A blank/placeholder command is skipped,
      so a non-empty stored command is never overwritten with an empty one.

    Like :func:`maybe_refresh_empty_build_config`, this is BEST-EFFORT: any
    exception is logged, the session rolled back, and NEVER raised into the
    merge path. Returns True iff a change was validated and persisted.
    """
    try:
        if not recovered_directories:
            return False

        from pydantic import ValidationError
        from sqlalchemy.orm.attributes import flag_modified

        from app.schemas import BuildConfig as BuildConfigSchema

        existing = dict(project.build_config or {})
        directories = [dict(d) for d in (existing.get("directories") or [])]
        index_by_path = {
            _normalize_dir_path(d.get("path")): i for i, d in enumerate(directories)
        }

        changed = False
        for recovered in recovered_directories:
            if not isinstance(recovered, dict):
                continue
            cmd = str(recovered.get("test_cmd") or "").strip()
            # CONSERVATIVE GUARD: never persist a blank/placeholder command, and
            # never overwrite a stored command with an empty one.
            if not cmd:
                continue
            norm = _normalize_dir_path(recovered.get("path"))
            rec_type = recovered.get("type")
            if norm in index_by_path:
                entry = directories[index_by_path[norm]]
                if entry.get("test_cmd") != cmd:
                    entry["test_cmd"] = cmd
                    changed = True
                # Fill in type only when the stored entry lacks one.
                if rec_type and not entry.get("type"):
                    entry["type"] = rec_type
                    changed = True
            else:
                new_entry: Dict[str, Any] = {"path": norm, "test_cmd": cmd}
                if rec_type:
                    new_entry["type"] = rec_type
                directories.append(new_entry)
                index_by_path[norm] = len(directories) - 1
                changed = True

        if not changed:
            return False

        existing["directories"] = directories
        try:
            validated = BuildConfigSchema(**existing)
            project.build_config = validated.model_dump(exclude_none=True)
        except ValidationError:
            # A recovered entry produced a shape the schema rejects (e.g. a
            # ``type`` outside the allowed set). Store raw so the corrected
            # command still takes effect and the operator can fix it in the UI
            # (mirrors the manual re-detect path's raw-store fallback).
            project.build_config = existing
        flag_modified(project, "build_config")
        await session.commit()
        logger.info(
            "[BUILD_CONFIG] Persisted recovered no-CI test command(s) for "
            "project %s: %s",
            getattr(project, "id", None),
            [_normalize_dir_path(d.get("path")) for d in recovered_directories],
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort, must never break the merge gate
        logger.warning(
            "[BUILD_CONFIG] Persisting recovered test command failed for "
            "project %s; leaving config unchanged",
            getattr(project, "id", None),
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 — keep the caller's session usable
            pass
        return False
