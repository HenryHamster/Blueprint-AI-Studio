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
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# In-memory per-project cooldown for expensive tree-walk re-detections.
# Keyed by str(project.id); value = monotonic timestamp of last attempt.
# Resets on process restart (rare, acceptable: burn is measured in minutes).
_redetect_no_test_cooldown: Dict[str, float] = {}
_redetect_empty_cooldown: Dict[str, float] = {}
# Cooldown for the bidirectional emptiness-disagreement re-detect path
# (maybe_redetect_on_emptiness_disagreement).
_redetect_disagreement_cooldown: Dict[str, float] = {}
# Cooldown for the stale-``repo_is_empty=False`` self-heal (the inverse walk of
# ``maybe_refresh_empty_build_config``): keyed by str(project.id).
_redetect_stale_empty_cooldown: Dict[str, float] = {}
# 6-hour window: a no-test repo walks the tree at most once per window.
_REDETECT_COOLDOWN_S: float = 6 * 3600.0


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
      ``protected_branches``, ``enable_self_improvement``, ``alert_domains``,
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
    if "alert_domains" in existing:
        detected["alert_domains"] = existing["alert_domains"]
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
    # The promotion-CI self-heal policy is operator-owned and never emitted by
    # repo detection; carry an explicit choice (including False) forward so a
    # manual re-detect / empty-config auto-refresh does not silently revert an
    # operator opt-out back to the default-ON.
    if "promotion_ci_selfheal_enabled" in existing:
        detected["promotion_ci_selfheal_enabled"] = existing[
            "promotion_ci_selfheal_enabled"
        ]
    # Per-project promotion rubric override (threshold/weights) and smoketest
    # opt-in are operator-owned and never emitted by repo detection; carry an
    # explicit choice forward so a manual re-detect / empty-config auto-refresh
    # does not silently revert the project to the fleet rubric (Fix 5).
    for _promo_key in (
        "promotion_threshold",
        "promotion_weights",
        "smoketest_enabled",
    ):
        if _promo_key in existing:
            detected[_promo_key] = existing[_promo_key]
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

        # Cooldown: avoid repeated BFS walks within the same window.
        _proj_id_key = str(getattr(project, "id", None) or "")
        _now_mono = time.monotonic()
        _last_attempt = _redetect_empty_cooldown.get(_proj_id_key)
        if _last_attempt is not None and (_now_mono - _last_attempt) < _REDETECT_COOLDOWN_S:
            return False
        if _proj_id_key:
            _redetect_empty_cooldown[_proj_id_key] = _now_mono

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


async def maybe_correct_stale_repo_empty_signal(project, session, *, ref=None) -> bool:
    """Self-heal a durable ``build_config.repo_is_empty=False`` that is stale on a
    genuinely empty/near-empty repo.

    This is the INVERSE of :func:`maybe_refresh_empty_build_config`. When a
    project's stored ``repo_is_empty`` is False but the repo is actually empty/
    near-empty, the agent's clone-time ``verifyCloneDelivered`` guard throws
    ``clone_failed`` on every task (durable-says-non-empty vs. live-tree-empty
    disagreement) and the project can NEVER self-correct — every task dies
    ``clone_failed`` forever (the live external-project wedge). This helper
    re-verifies the durable signal against GitHub on the disagreement path and
    flips it to True ONLY when fresh detection authoritatively confirms the repo
    is empty.

    Fail-loud is preserved: the flip is gated on an AUTHORITATIVE empty verdict
    (``detected['repo_is_empty'] is True`` AND ``detected['source_file_count'] is
    not None`` — a complete, successful listing). A transient GitHub failure
    (detector returns ``{}``), an indeterminate/partial read (``source_file_count
    is None``), or a repo that genuinely HAS content the clone failed to deliver
    (``repo_is_empty`` stays False) all leave the stored signal untouched, so a
    real clone shortfall keeps failing loud instead of being masked as greenfield.

    Fires ONLY while stored ``repo_is_empty`` is exactly False; a config that is
    already True, absent, or non-boolean is left untouched. Bounded by the same
    per-project ``_REDETECT_COOLDOWN_S`` window used by the sibling re-detect
    helpers so the BFS tree walk never fires on every disagreement report.

    Returns True iff the signal was corrected and persisted, else False.
    Best-effort: never raises into the caller; logs, rolls back the session on
    failure, and returns False.
    """
    try:
        existing = project.build_config or {}
        # Only fire when the durable signal is EXACTLY False. True/absent/non-bool
        # need no correction, and firing on them would either be a no-op walk or
        # risk flipping a legitimately-non-empty repo.
        if existing.get("repo_is_empty") is not False:
            return False
        if not getattr(project, "github_owner", None) or not getattr(
            project, "github_repo", None
        ):
            return False

        # Cooldown: one bounded re-detection per project per window, so a project
        # that dies clone_failed repeatedly does not walk the tree every report.
        _proj_id_key = str(getattr(project, "id", None) or "")
        _now_mono = time.monotonic()
        _last_attempt = _redetect_stale_empty_cooldown.get(_proj_id_key)
        if _last_attempt is not None and (_now_mono - _last_attempt) < _REDETECT_COOLDOWN_S:
            return False
        # Stamp before any API call so concurrent callers in the same window skip
        # rather than duplicate the expensive walk.
        if _proj_id_key:
            _redetect_stale_empty_cooldown[_proj_id_key] = _now_mono

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
            # Transient GitHub failure (detector returned {}). Leave the durable
            # signal untouched — fail-loud is preserved for the clone shortfall.
            return False

        # AUTHORITATIVE empty verdict required to flip. A repo that genuinely has
        # content (repo_is_empty False) or an indeterminate/partial read
        # (source_file_count None) must NOT correct the signal, so a real clone
        # shortfall keeps failing loud.
        if not (
            detected.get("repo_is_empty") is True
            and detected.get("source_file_count") is not None
        ):
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
            "[BUILD_CONFIG] Self-healed stale repo_is_empty=False for project %s "
            "(integration branch %s) — fresh detection authoritatively confirms "
            "the repo is empty (source_file_count=%s); next task proceeds greenfield",
            getattr(project, "id", None),
            integration_branch,
            detected.get("source_file_count"),
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort, must never break the report path
        logger.warning(
            "[BUILD_CONFIG] Self-heal of stale repo_is_empty failed for project "
            "%s; leaving config unchanged",
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

    A 6-hour in-memory cooldown prevents the BFS tree walk from re-running on
    every orchestrator cycle for repos that genuinely have no test command.

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

        # Cooldown: skip if last attempt was within _REDETECT_COOLDOWN_S.
        # Prevents the BFS tree walk from firing on every orchestrator cycle
        # for repos that genuinely have no test command.
        _proj_id_key = str(getattr(project, "id", None) or "")
        _now_mono = time.monotonic()
        _last_attempt = _redetect_no_test_cooldown.get(_proj_id_key)
        if _last_attempt is not None and (_now_mono - _last_attempt) < _REDETECT_COOLDOWN_S:
            return False
        # Stamp before any API call so concurrent callers in the same window
        # skip rather than duplicate the expensive walk.
        if _proj_id_key:
            _redetect_no_test_cooldown[_proj_id_key] = _now_mono

        # Quota reserve: skip when the install-bucket is near exhaustion so
        # advisory re-detection never starves critical PR/merge/CI machinery.
        from app.github_repo_snapshot import _quota_reserve_active

        if _quota_reserve_active():
            logger.debug(
                "[BUILD_CONFIG] Skipping no-test redetect for project %s — quota reserve active",
                getattr(project, "id", None),
            )
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


async def maybe_redetect_ci_presence(project, session, *, ref=None) -> bool:
    """Best-effort re-detection of CI presence for a project whose stored
    ``build_config.has_ci`` is None/absent.

    A stale ``has_ci=None`` (onboarding detection was incomplete, or the project
    predates the ``has_ci`` tri-state) makes :func:`ci_resolution.proceed_on_no_checks`
    return False, so a project that genuinely has no CI strands every
    main-targeting PR forever — the no-checks poll never resolves and each rollup
    must be hand-merged.  This helper attempts a single bounded re-detection using
    the same detector and operator-override-preserving path as
    :func:`maybe_redetect_missing_test_command`, then persists the freshly-detected
    ``has_ci`` so ``proceed_on_no_checks`` can resolve the strand on this or the
    next poll.  Because it self-backfills stuck projects on the next poll, a
    separate data-migration backfill is unnecessary.

    Fires ONLY when stored ``has_ci`` is None/absent; a project that already
    carries a DEFINITIVE ``has_ci`` (True or False) is left untouched.  Persists
    ONLY when re-detection yields a definitive ``has_ci`` (True or False) — a
    still-incomplete read (``has_ci`` None) leaves the config unchanged so a
    guessed value is never recorded and the strand keeps polling.

    Returns True iff re-detection yielded a definitive ``has_ci`` and persisted it.
    Best-effort: never raises into the caller; logs, rolls back the session on
    failure, and returns False.
    """
    try:
        existing = project.build_config or {}
        # Only fire for a stale/absent tri-state; never overwrite a definitive
        # has_ci (True or False) already on the config.
        if existing.get("has_ci") is not None:
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

        # Only persist when detection produced a DEFINITIVE has_ci. A still
        # incomplete read (has_ci None) must not overwrite the stored config —
        # the strand keeps polling rather than recording a guessed value.
        if detected.get("has_ci") is None:
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
            "[BUILD_CONFIG] Re-detected CI presence for project %s (has_ci=%s) — "
            "no-CI strand can now resolve",
            getattr(project, "id", None),
            detected.get("has_ci"),
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort, must never block a merge path
        logger.warning(
            "[BUILD_CONFIG] CI-presence re-detection failed for project %s; "
            "leaving config unchanged",
            getattr(project, "id", None),
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 — keep the caller's session usable
            pass
        return False


async def maybe_redetect_on_emptiness_disagreement(
    project, session, *, observed_empty: bool, ref=None
) -> bool:
    """Re-run authoritative detection when the agent-observed repo emptiness
    disagrees with the durable ``build_config.repo_is_empty`` signal in EITHER
    direction.

    The durable ``repo_is_empty`` is a shallow onboarding-time signal that can
    drift from reality in two ways once the agent VM does a full clone:

    - Durable says NOT empty (``repo_is_empty=False``) but the agent's full
      clone sees an empty/near-empty tree (``observed_empty=True``) — the repo
      may have been mis-classified at onboarding, or emptied since.
    - Durable says empty (``repo_is_empty=True``) but the agent sees real source
      (``observed_empty=False``) — the greenfield repo gained structure the
      shallow onboarding walk never re-scanned, and the durable signal keeps
      advertising a greenfield repo (short-circuiting bootstrap-from-goal
      guidance and greenfield gates).

    Rather than hard-failing (direction 1 currently throws ``clone_failed``) or
    silently trusting the stale durable signal (direction 2), this helper
    re-runs the SAME authoritative GitHub detector (``_detect_build_config``)
    the onboarding path used and persists its fresh verdict — so the
    disagreement is resolved by the authoritative source, never by blindly
    trusting either the stale durable signal or the agent's live tree.

    Guards, mirroring the sibling re-detect helpers:

    - Fires ONLY on a genuine disagreement: the durable ``repo_is_empty`` must
      be a DEFINITE bool that differs from ``observed_empty``. An absent/None
      durable tri-state is indeterminate (there is nothing to disagree with) and
      is left to :func:`maybe_refresh_empty_build_config`, so the two paths never
      compete.
    - A 6-hour in-memory per-project cooldown prevents the BFS tree walk from
      re-running on every agent report within the window.
    - Reuses the operator-override-preserving merge, so operator-owned fields
      survive; the fresh ``repo_is_empty``/``source_file_count`` are NOT carried
      forward from the stale config and therefore win.

    Returns True iff a fresh detection was persisted. Best-effort: never raises
    into the caller; logs, rolls back the session on failure, and returns False.
    """
    try:
        existing = project.build_config or {}
        durable = existing.get("repo_is_empty")
        # Only a DEFINITE durable bool can disagree; a None/absent tri-state is
        # indeterminate and belongs to maybe_refresh_empty_build_config.
        if not isinstance(durable, bool):
            return False
        if bool(durable) == bool(observed_empty):
            # Signals agree — nothing to reconcile.
            return False
        if not getattr(project, "github_owner", None) or not getattr(
            project, "github_repo", None
        ):
            return False

        # Cooldown: avoid repeated BFS walks within the same window.
        _proj_id_key = str(getattr(project, "id", None) or "")
        _now_mono = time.monotonic()
        _last_attempt = _redetect_disagreement_cooldown.get(_proj_id_key)
        if _last_attempt is not None and (
            _now_mono - _last_attempt
        ) < _REDETECT_COOLDOWN_S:
            return False
        # Stamp before any API call so concurrent callers in the same window skip
        # rather than duplicate the expensive walk.
        if _proj_id_key:
            _redetect_disagreement_cooldown[_proj_id_key] = _now_mono

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
            "[BUILD_CONFIG] Re-detected repo emptiness for project %s after "
            "agent/backend disagreement (durable repo_is_empty=%s, agent "
            "observed_empty=%s) → fresh repo_is_empty=%s",
            getattr(project, "id", None),
            durable,
            observed_empty,
            detected.get("repo_is_empty"),
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort, must never break the agent run
        logger.warning(
            "[BUILD_CONFIG] Emptiness-disagreement re-detection failed for "
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
