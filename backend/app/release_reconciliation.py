"""Reconciliation helpers for release qualification sweeps.

This module owns the out-of-band reconciliation paths that catch a release
version up when its promotion PR lands (or is terminally closed) outside the
normal qualification completion flow.
"""

from __future__ import annotations


import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app import github, pause_clock, release_version
from app.database import AsyncSessionLocal
from app.github_checks import _details_have_genuine_failure
from app.models import (
    DeploymentState,
    Project,
    Task,
    TaskStatus,
)
from app.release_artifacts import (
    aggregate_required_artifact_status,
    is_backend_revision_artifact,
)
from app.release_merge_refusal import promotion_merge_refusal
from app.release_promotion import (
    _candidate_already_on_main,
    _delete_release_branch_best_effort,
    _idempotent_pr_url,
    _lts_branch_names,
    _record_promotion_failure,
    _record_promotion_success,
    _recover_confidence_from_ci_unresolved,
    _resolve_exact_promotion_receipt,
    _resolve_github_with_session,
)
from app.release_promotion_pending import (
    _MANAGED_CHECK_WAIT_SNAPSHOT_DETAIL_LIMIT,
    _PROMOTION_PENDING_ABSENT,
    _PROMOTION_PENDING_CLAIM,
    _PROMOTION_PENDING_MALFORMED,
    _PROMOTION_PENDING_MANAGED,
    _canonicalize_promotion_pending,
    _classify_405_message,
    _increment_pending_transient_red_count,
    _managed_check_wait_snapshot,
    _parse_promotion_pending_time,
    _persist_managed_check_wait_snapshot,
    _promotion_pending_fallback_enabled_at,
)
from app.rollup_migration_validation import _validate_migration_chain_post_merge
from app.ci_resolution import (
    _CI_GREEN,
    _CI_NO_CHECKS,
    _CI_PENDING,
    _CI_RED,
    _classify_check_runs,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

# Re-exports — the post-promotion serving-confirmation cluster lives in the
# sibling module ``app.release_serving_confirmation`` (extracted to keep this
# module under its size cap).  The names below remain importable from here so
# existing callers (release_qualifier_sweep, admin_release_version, and the
# test suites) keep resolving them through the canonical module.
from app.release_serving_confirmation import (
    _DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS_DEFAULT,  # noqa: F401 — re-exported for back-compat
    _classify_serving_outcome,  # noqa: F401 — re-exported for back-compat
    _confirm_serving_or_fail,  # noqa: F401 — re-exported for back-compat
    _reconcile_if_deploy_failed_recovered,  # noqa: F401 — re-exported for back-compat
    _record_activation_receipt,  # noqa: F401 — re-exported for back-compat
    _record_post_promotion_outcome,  # noqa: F401 — re-exported for back-compat
    _recover_serving_confirmed,  # noqa: F401 — re-exported for back-compat
    _refresh_release_artifact_rows,  # noqa: F401 — re-exported for back-compat
    _to_naive_utc,  # noqa: F401 — re-exported for back-compat
)

logger = logging.getLogger(__name__)
_MODULE_SIZE_GOVERNANCE_CAP: int = 2841

# Audit C9: hard wall-clock deadline on the ``promotion_pending`` sub-state.
#
# When a qualification PASSES and promotion fires but the promotion PR's CI is
# still pending (GitHub 405, no conflict), ``promote()`` leaves
# ``last_qualification.status == "pending"`` with a managed
# ``promotion_pending`` sub-state so Blueprint can retry later. By design that state
# *bypasses* the 6h qualification-timeout backstop
# (``_finalize_in_dedicated_session`` returns early when ``promotion_pending`` is
# present) — the qualifier children already terminated, so that backstop no
# longer applies and killing the version would discard a passing PR mid-CI.
#
# The managed reconciler retries an exact target; this separate deadline keeps
# persistent PR/API failure from stranding the self-improve loop indefinitely.
#
# This deadline is the replacement backstop.  Once a managed PR has aged past
# the cap, the sweeper first persists cleanup debt, then classifies GitHub's
# authoritative PR state.  An exact merged receipt records success; a closed
# unmerged PR or an open PR that can be closed records timeout failure; unknown
# state or failed closure retains the debt and the qualification fence for the
# next sweep.  The default (24h) is deliberately well beyond the 4h stall alert
# and any realistic CI duration so it only fires on a genuinely wedged
# promotion, never on a slow-but-progressing one.
_PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT = 24 * 3600
# A bare pre-PR claim is written under-lock before GitHub I/O so concurrent
# qualification cannot clobber the in-flight promotion. On straight-through success
# path promote() writes NO intermediate pr_number record — it goes claim →
# create_branch → create_pull_request → merge → _record_promotion_success — so
# the DB shows a bare claim for the ENTIRE promote() I/O, not merely between
# claiming and opening the PR.  This bound must therefore sit safely ABOVE the
# worst-case whole-I/O duration (nominally 30-120s, but GitHub secondary
# rate-limit Retry-After backoff can stack across ~6 calls), or the reaper would
# tear down a LIVE, slow promotion and REOPEN the clobber race this claim closes.
# 30 min is far beyond any realistic promote() I/O yet far below the 24h cap a
# real open-PR promotion inherits, so a genuinely crash-orphaned claim is still
# GC'd promptly.  DO NOT lower this toward the "PR opens in seconds" figure — the
# bare-claim window is the WHOLE I/O, not the pre-PR sliver.
_PROMOTION_CLAIM_TIMEOUT_SECONDS = 30 * 60
# Number of consecutive sweeps where CI is red due to only transient conclusions
# (cancelled/timed_out) before the reconciler escalates to the normal terminal
# path.  Overridable via runtime_config["max_transient_ci_red_sweeps"].
_TRANSIENT_CI_RED_SWEEP_DEFAULT = 5
# Number of consecutive sweeps where the GitHub check-runs API returns an
# error before the reconciler surfaces a distinct warning so operators know
# that a green-but-stuck appearance may be an API-error masquerade.
_API_ERROR_SWEEP_WARN_THRESHOLD = 3




def _aggregate_non_backend_required_artifact_status(
    provider_rows,
) -> tuple[str, str | None]:
    """Aggregate required artifacts excluding backend_revision."""

    return aggregate_required_artifact_status(
        [
            (provider, artifact)
            for provider, artifact in provider_rows
            if provider.required
            and not is_backend_revision_artifact(provider.artifact_kind)
        ]
    )


def _promotion_pending_timeout_seconds() -> float:
    """Resolve the ``promotion_pending`` wall-clock cap (configurable).

    Read from ``runtime_config['promotion_pending_timeout_seconds']`` when set
    to a positive number, else the 24h default.  A non-positive / unparseable
    override is ignored so a bad config value can never disable the backstop.
    """
    raw = _runtime_config.get("promotion_pending_timeout_seconds")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT
    if value <= 0:
        return _PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT
    return value


def _max_transient_ci_red_sweeps() -> int:
    """Resolve the configurable cap on consecutive transient-only-red CI sweeps.

    Read from ``runtime_config['max_transient_ci_red_sweeps']`` when set to a
    positive integer, else the default.  A non-positive / unparseable value is
    ignored so a bad config can never disable the escalation backstop.
    """
    raw = _runtime_config.get("max_transient_ci_red_sweeps")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _TRANSIENT_CI_RED_SWEEP_DEFAULT
    if value <= 0:
        return _TRANSIENT_CI_RED_SWEEP_DEFAULT
    return value


def _check_result_has_genuine_failure(check_result: dict | None) -> bool:
    """Return True when *check_result* contains at least one genuinely failed check.

    Genuine failure conclusions: ``failure``, ``action_required``.
    Transient interruption conclusions: ``cancelled``, ``timed_out``.

    When per-check ``details`` are present and non-empty the verdict is derived
    from individual check conclusions via
    :func:`app.github_checks._details_have_genuine_failure`, so a transient-only
    outcome is correctly excluded — INCLUDING an aggregate ``needs`` gate whose
    ``failure`` only launders a cancelled/timed_out matrix shard (the v28
    attempt-6 promotion failure on PR #16885, where ``CI / Backend`` reported
    ``failure`` with zero failing tests).

    The ``genuine_failure`` signal computed at the ``get_check_runs`` boundary is
    trusted when present and explicitly ``False`` (an aggregate-parent-aware
    computation the details-only fallback mirrors); when the signal is absent
    (hand-built fixtures / legacy callers) the details fallback applies the same
    discount.  When details are absent or empty the aggregate ``conclusion``
    field is used as a fallback — preserving existing behaviour for callers that
    supply no per-check detail list.
    """
    if not isinstance(check_result, dict):
        return False
    # The get_check_runs boundary computes the aggregate-parent-aware signal from
    # the SAME details list below, so a False here is authoritative even when the
    # aggregate conclusion is failure.
    if check_result.get("genuine_failure") is False:
        return False
    genuine = {"failure", "action_required"}
    details = check_result.get("details")
    if isinstance(details, list) and details:
        return _details_have_genuine_failure(details)
    # No per-check details — fall back to the aggregate conclusion.
    return check_result.get("conclusion") in genuine


def _ci_result_has_failed_test(check_result: dict | None) -> bool:
    """Return True when *check_result* contains a genuinely FAILED test that a
    code-fix task could remediate — a leaf (non-aggregate-parent) check whose
    conclusion is ``failure``.

    This is the promotion-CI harvest's "is there anything to fix" signal.  It is
    deliberately stricter than :func:`_check_result_has_genuine_failure`: an
    ``action_required`` check is a human-action gate (review / approval) and a
    ``cancelled`` / ``timed_out`` is infrastructure — neither is a failing test a
    code change can fix (the auto-remediation handler likewise only treats
    ``failure`` as a failed check).  When the promotion PR's red CI is made up
    entirely of those (the infrastructure-cancelled-shard case), the harvest must
    record the failure as infrastructure and NOT spawn a doomed code-fix task.

    Mirror of :func:`_check_result_has_genuine_failure`: an aggregate ``needs``
    gate whose ``failure`` merely launders a cancelled/timed_out matrix shard is
    discounted (a parent name with ``X (``-prefixed children never counts), so a
    laundered aggregate cannot fool the harvest into spawning a fix.
    """
    if not isinstance(check_result, dict):
        return False
    details = check_result.get("details")
    if isinstance(details, list) and details:
        names = [str(d.get("name") or "") for d in details if isinstance(d, dict)]
        for d in details:
            if not isinstance(d, dict) or d.get("conclusion") != "failure":
                continue
            name = str(d.get("name") or "")
            if any(other != name and other.startswith(f"{name} (") for other in names):
                # Aggregate parent — its `failure` launders its matrix children.
                continue
            return True
        return False
    # No per-check details — fall back to the aggregate conclusion.
    return check_result.get("conclusion") == "failure"


def _pr_number_from_url(pr_url: str | None) -> int | None:
    """Parse a GitHub pull-request number from its URL, if possible."""
    if not isinstance(pr_url, str) or not pr_url:
        return None
    try:
        return int(pr_url.rstrip("/").split("/")[-1])
    except ValueError:
        return None


async def _reconcile_if_promotion_failed_landed(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Reconcile an open version whose promotion-failure candidate is already on main."""
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            # Lock the version row — same serialization point as _finalize_locked_version.
            version: release_version.ReleaseVersion | None = (
                (
                    await session.execute(
                        select(release_version.ReleaseVersion)
                        .where(release_version.ReleaseVersion.id == version_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                return False
            if version.status != "open":
                return False
            qual = version.last_qualification or {}
            if qual.get("status") != "promotion_failed":
                return False
            candidate_sha: str | None = qual.get("candidate_sha")
            if not candidate_sha:
                return False

            base_record = dict(qual)
            project = await session.get(Project, version.project_id)
            if project is None:
                return False

            # Single-branch repo guard (LTS model inactive) — mirrors the
            # promote() guard.  When the integration branch IS the promotion
            # target there is no separate release trunk: fleet work (including
            # this candidate) already merges directly to that branch, so
            # promote() records an inert success and never opens a release PR.
            # _candidate_already_on_main would therefore ALWAYS be True on the
            # single branch — reconciling here would spuriously record a
            # promotion against a branch work already lands on.  Bail out inert,
            # mirroring the back-merge guard's "lts_inactive" skip semantics.
            integration_branch, promotion_target = _lts_branch_names(project)
            if integration_branch == promotion_target:
                logger.debug(
                    "[RELEASE_QUALIFIER] reconcile: v%d integration branch (%s) "
                    "is the promotion target — single-branch repo, LTS inactive; "
                    "nothing to reconcile",
                    version.version_number,
                    promotion_target,
                )
                return False

            # Release the row lock before any GitHub I/O (mirrors promote()).
            try:
                await session.commit()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile: failed to release row lock "
                    "for v%d before GitHub I/O — skipping",
                    version.version_number,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] reconcile: rollback failed for v%d "
                        "after commit error",
                        version.version_number,
                    )
                return False

            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile: no GitHub credentials for "
                    "v%d — skipping reconcile",
                    version.version_number,
                )
                return False
            token, owner, repo = resolved

            pr_number_hint = _pr_number_from_url(base_record.get("pr_url"))
            receipt = await _resolve_exact_promotion_receipt(
                token,
                owner,
                repo,
                candidate_sha,
                version,
                project=project,
                pr_number=pr_number_hint,
            )
            if receipt is None:
                return False

            pr_number = receipt.get("pr_number")
            if pr_number is None:
                alembic_status, alembic_reason = "ok", "empty diff"
            else:
                alembic_status, alembic_reason = (
                    await _validate_migration_chain_post_merge(
                        token,
                        owner,
                        repo,
                        int(pr_number),
                        receipt["merge_commit_sha"],
                    )
                )
            if alembic_status == "defer":
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile: candidate %s for v%d is "
                    "on main, but post-merge Alembic health check is deferred: %s",
                    candidate_sha[:8],
                    version.version_number,
                    alembic_reason,
                )
                return False
            if alembic_status == "block":
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile: candidate %s for v%d is "
                    "on main, but the live target Alembic chain has a genuine "
                    "third-party collision — recording promotion_failed: %s",
                    candidate_sha[:8],
                    version.version_number,
                    alembic_reason,
                )
                await _record_promotion_failure(
                    session,
                    version,
                    base_record,
                    "migration_guard_blocked",
                    detail=alembic_reason,
                    pr_url=_idempotent_pr_url(base_record),
                    lock_released=True,
                )
                await session.commit()
                return True

            logger.info(
                "[RELEASE_QUALIFIER] reconcile: candidate %s for v%d is on "
                "main despite promotion_failed record — reconciling as promoted",
                candidate_sha[:8],
                version.version_number,
            )

            result = await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                pr_url=receipt.get("pr_url") or _idempotent_pr_url(base_record),
                token=token,
                owner=owner,
                repo=repo,
                empty_diff=bool(receipt.get("empty_diff")),
                promoted_sha=receipt["merge_commit_sha"],
                already_landed=True,
                lock_released=True,
            )
            await session.commit()
            return result.get("status") == "promoted" and not result.get(
                "already_promoted"
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile: unexpected error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile: rollback failed for version %s "
                    "after unexpected error",
                    version_id,
                )
            return False


async def _reconcile_if_not_promoted_but_pr_merged(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Reconcile an open version recorded as not_promoted/ci_unresolved whose PR merged.

    Incident 2026-06-09: a transient DB error rolled back the promotion_pending
    marker write after a release PR was created. A subsequent finalize saw no
    promotion_pending, re-evaluated CI as
    ci_unresolved (a transient state, not a hard failure), and recorded
    not_promoted — even though the PR merged 15 seconds later.

    For every open version whose last_qualification shows
    status=="not_promoted" AND blocker=="ci_unresolved", checks whether the
    candidate is already reachable from the promotion target. If it is, the PR
    merged and the not_promoted record is incorrect — drive the version to
    _record_promotion_success. Hard blockers (ci_not_green, migration collision,
    audit quality) are intentionally left alone.

    Follows the same pattern as _reconcile_if_promotion_failed_landed.
    Returns True iff the version was reconciled to promoted.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            version: release_version.ReleaseVersion | None = (
                (
                    await session.execute(
                        select(release_version.ReleaseVersion)
                        .where(release_version.ReleaseVersion.id == version_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                return False
            if version.status != "open":
                return False
            qual = version.last_qualification or {}
            if qual.get("status") != "not_promoted":
                return False
            # Only reconcile the transient ci_unresolved class.  Hard blockers
            # (ci_not_green, migration_identity_collision, audit quality, etc.)
            # represent a genuine scoring failure — never reconcile as success.
            if qual.get("blocker") != "ci_unresolved":
                return False
            candidate_sha: str | None = qual.get("candidate_sha")
            if not candidate_sha:
                return False

            base_record = _recover_confidence_from_ci_unresolved(dict(qual))
            project = await session.get(Project, version.project_id)
            if project is None:
                return False

            # Single-branch repo guard — mirrors promote().
            integration_branch, promotion_target = _lts_branch_names(project)
            if integration_branch == promotion_target:
                logger.debug(
                    "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: v%d "
                    "integration branch (%s) is the promotion target — single-branch "
                    "repo, LTS inactive; nothing to reconcile",
                    version.version_number,
                    promotion_target,
                )
                return False

            # Release the row lock before GitHub I/O.
            try:
                await session.commit()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: "
                    "failed to release row lock for v%d — skipping",
                    version.version_number,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: "
                        "rollback failed for v%d after commit error",
                        version.version_number,
                    )
                return False

            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: "
                    "no GitHub credentials for v%d — skipping",
                    version.version_number,
                )
                return False
            token, owner, repo = resolved

            receipt = await _resolve_exact_promotion_receipt(
                token,
                owner,
                repo,
                candidate_sha,
                version,
                project=project,
                pr_number=_pr_number_from_url(base_record.get("pr_url")),
            )
            if receipt is None:
                return False

            receipt_pr_number = receipt.get("pr_number")
            if receipt_pr_number is not None:
                alembic_status, alembic_reason = (
                    await _validate_migration_chain_post_merge(
                        token,
                        owner,
                        repo,
                        int(receipt_pr_number),
                        receipt["merge_commit_sha"],
                    )
                )
                if alembic_status == "defer":
                    return False
                if alembic_status == "block":
                    await _record_promotion_failure(
                        session,
                        version,
                        base_record,
                        "migration_guard_blocked",
                        detail=alembic_reason,
                        pr_url=receipt.get("pr_url"),
                        lock_released=True,
                    )
                    await session.commit()
                    return True

            logger.info(
                "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: "
                "candidate %s for v%d is on main despite not_promoted/ci_unresolved "
                "record — reconciling as promoted",
                candidate_sha[:8],
                version.version_number,
            )
            result = await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                pr_url=receipt.get("pr_url") or _idempotent_pr_url(base_record),
                token=token,
                owner=owner,
                repo=repo,
                empty_diff=bool(receipt.get("empty_diff")),
                promoted_sha=receipt["merge_commit_sha"],
                already_landed=True,
                lock_released=True,
            )
            await session.commit()
            return result.get("status") == "promoted" and not result.get(
                "already_promoted"
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: "
                "unexpected error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: "
                    "rollback failed for version %s after unexpected error",
                    version_id,
                )
            return False


def promotion_ci_selfheal_enabled(project: Project) -> bool:
    """Whether the engine self-heals THIS project's release-promotion CI failures.

    This governs the promotion-CI harvest (``_harvest_promotion_ci_failures``) —
    the engine repairing its OWN release pipeline when a promotion PR's full CI
    matrix goes red.  It is a release-pipeline concern and is deliberately
    DECOUPLED from the generic protected-branch auto-remediation gate
    (``design_doc._auto_remediation.enabled``), which the strategist rewrites
    every cycle and which therefore does not durably persist an operator's
    intent.

    Resolution (durable + strategist-proof + project-agnostic):

    * An explicit operator choice in operator-owned ``build_config``
      (``promotion_ci_selfheal_enabled``) wins.  ``build_config`` is never
      written by the strategist (it writes ``design_doc`` / ``problem_model``),
      so an operator opt-out/opt-in survives every strategic cycle, and the
      ``BuildConfig`` schema + ``apply_operator_build_config_overrides`` carry it
      through onboard / PATCH / re-detect.
    * Otherwise it defaults ON.  No self-repo flag, no repo-name special-casing:
      the harvest only ever runs when a promotion PR's CI fails (i.e. the project
      is actively promoting releases), so a blanket default is self-limiting and
      correct for any project.  The existing safety guards (infra-flake
      detection, per-hour / concurrent rate limits, branch suspension,
      require-review) still apply to whatever fix it spawns.
    """
    val = (project.build_config or {}).get("promotion_ci_selfheal_enabled")
    if isinstance(val, bool):
        return val
    return True


async def _harvest_promotion_ci_failures(
    *,
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    version_number: int,
    owner: str,
    repo: str,
    ci_source_sha: str,
    integration_branch: str,
    auto_remediation_config: dict[str, Any],
    selfheal_enabled: bool,
    flake_rerun_budget: int | None = None,
) -> str:
    """Spawn a CI auto-remediation fix task — on the integration branch — for a
    just-failed promotion PR's CI failures.

    The promotion PR runs the full CI matrix on a throwaway ``release-v*`` branch.
    When it goes red the release branch is torn down and the version returns to
    stabilize, but nothing reads those failures: the auto-remediation webhook
    whitelist only covers the integration branch and the promotion target, never
    the release branch (``routers/github.py``).  So the next qualification
    re-promotes the same code and fails the same CI — a promotion-gate loop
    (this is the side-effect of dropping CI on the integration branch: the full
    matrix now runs only here).  We feed the failing-test context — read from
    ``ci_source_sha`` (the release commit) — into the standard remediation handler
    but target the fix at ``integration_branch`` so it lands where the next
    qualification will pick it up.

    ``selfheal_enabled`` is the release-level gate
    (``promotion_ci_selfheal_enabled(project)``), resolved by the caller from
    durable operator-owned ``build_config`` — NOT from the strategist-clobbered
    ``design_doc._auto_remediation.enabled``.  ``auto_remediation_config`` is
    still threaded through to ``_handle_main_branch_ci_failure`` for the SPAWN
    MECHANICS only (per-hour / concurrent caps, branch suspension), and it safely
    falls back to built-in defaults when the project never customized it.

    ``flake_rerun_budget`` distinguishes the two harvest entry points AND bounds
    the rerun loop.  It is tri-state:

    * ``None`` — the promotion-failure re-driver.  By the time it harvests, the
      release branch has been deleted and re-running the dead workflow is
      useless, so an infra flake is handed to Strategic without any rerun.
    * ``> 0`` — the standing drift sentinel
      (``release_qualifier_sweep._check_full_suite_drift_sentinel``) with rerun
      budget remaining.  The CI run it harvests lives on the LIVE integration
      branch, so re-running a flake's failed jobs can clear the same HEAD's red
      gate without waiting for a new commit.
    * ``0`` — the drift sentinel with its per-HEAD rerun budget EXHAUSTED.  The
      sentinel re-runs this harvest on every sweep while the gate is red, and
      ``_CI_INFRA_LOG_PATTERNS`` matches broad substrings against the whole
      aggregated log, so a genuine regression can be misclassified as a flake.
      Without a budget that would rerun the full matrix forever and never
      escalate.  At zero budget the flake verdict is treated as un-actionable
      and we FALL THROUGH to the fix spawn, exactly as
      ``ci_completion``'s ``ci_infra_rerun_done`` / ``_CI_RERUN_BUDGET`` gates
      do when their one rerun fails to clear the red.

    The caller owns budget accounting (keyed on HEAD sha so a new commit gets a
    fresh budget); the return value tells it what happened.  Returns one of:
    ``"selfheal_disabled"``, ``"flake_rerun"`` (a rerun was actually issued —
    consume budget), ``"flake_handoff"`` (flake, no rerun issued),
    ``"fix_spawned"``, or ``"error"``.

    Best-effort: the promotion failure is already recorded and committed by the
    caller, so any error here is logged and swallowed — a harvest failure must
    never turn a recorded promotion failure into an unhandled exception.  The
    remediation handler self-gates: if ``ci_source_sha`` has no failed checks it
    no-ops, so calling this on a non-CI promotion failure is harmless.
    """
    try:
        if not selfheal_enabled:
            logger.info(
                "[RELEASE_QUALIFIER] promotion-CI harvest: self-heal disabled "
                "(build_config.promotion_ci_selfheal_enabled=False) for project "
                "%s — not spawning a fix for v%d CI failures",
                project_id,
                version_number,
            )
            return "selfheal_disabled"

        # ── Infra-flake guard (Fix X) ──────────────────────────────────────────
        # Before spawning a code-fix task, check whether the promotion CI failure
        # is a transient infrastructure flake (npm registry unreachable,
        # Docker Hub rate-limit, Playwright install failure, setup-step only).
        # If so, skip the fix spawn and hand the new delivery decision to
        # Strategic — a code change cannot fix an infra flake, and spawning a
        # doomed repair agent wastes a retry slot.
        #
        # NOTE: by the time this harvest runs, _cleanup_managed_check_wait_failure_
        # artifacts has already closed the promotion PR and deleted the release
        # branch.  Re-running the dead workflow via rerun_failed_jobs is useless;
        # instead, arm the coalesced Strategic refresh marker from this terminal
        # promotion-failure edge. The harvest is invoked for the failure event,
        # not from a standing-state sweep, and the marker is single-slot, so this
        # cannot accumulate repeated wakes for persistent state.
        #
        # ``flake_rerun_budget`` bounds the live-branch rerun (see the docstring):
        # None = re-driver (never rerun, always hand off), >0 = sentinel with
        # budget, 0 = sentinel out of budget -> escalate the flake to a fix.
        _flake_budget_remaining = max(0, flake_rerun_budget or 0)
        _flake_budget_exhausted = (
            flake_rerun_budget is not None and _flake_budget_remaining == 0
        )
        _flake_outcome = "flake_handoff"
        try:
            from app.github import get_repo_installation_token, get_check_runs_cached
            from app.failure_classification import _is_ci_infra_flake_log

            _token = await get_repo_installation_token(owner, repo)
            if _token:
                _ci_result = await get_check_runs_cached(
                    _token,
                    owner,
                    repo,
                    ci_source_sha,
                    project_id=str(project_id),
                )
                _setup_flake = _ci_result.get("setup_phase_failure", False)
                _failed_logs = _ci_result.get("failed_logs", "")
                _content_flake = _is_ci_infra_flake_log(_failed_logs)
                # Zero genuinely failing tests — the promotion PR went red only on
                # infrastructure-cancelled/timed_out shards (or a laundered
                # aggregate gate).  A code change cannot remediate this, so the
                # harvest must never spawn a fix task that has nothing to fix;
                # record it as infrastructure and hand the decision to Strategic.
                _no_failed_tests = not _ci_result_has_failed_test(_ci_result)
                if (
                    (_setup_flake or _content_flake)
                    and _flake_budget_exhausted
                    and not _no_failed_tests
                ):
                    # Rerun budget for THIS head_sha is spent and the gate is
                    # still red, so the flake verdict is not actionable — either
                    # the infra problem is persistent or (more likely, given the
                    # broad substring patterns) a genuine regression was
                    # misclassified.  Escalate to the fix path rather than
                    # re-running the full matrix on every sweep forever.  Same
                    # fall-through the ci_completion rerun gates take when their
                    # one rerun fails to clear the red.
                    logger.warning(
                        "[RELEASE_QUALIFIER] promotion-CI harvest: v%d CI failure "
                        "still looks like an infrastructure flake (%s) but the "
                        "per-HEAD rerun budget is exhausted for %s — escalating "
                        "to a fix spawn instead of re-running again",
                        version_number,
                        "setup_phase_failure" if _setup_flake else "infra_log_patterns",
                        ci_source_sha[:8],
                    )
                elif _setup_flake or _content_flake or _no_failed_tests:
                    if _no_failed_tests:
                        _flake_reason = "zero_failing_tests"
                        logger.info(
                            "[RELEASE_QUALIFIER] promotion-CI harvest: v%d "
                            "promotion CI red reports ZERO genuinely failing "
                            "tests (infrastructure-cancelled/timed_out shards "
                            "only) — recording as infrastructure and skipping "
                            "fix spawn (source %s)",
                            version_number,
                            ci_source_sha[:8],
                        )
                    else:
                        _flake_reason = (
                            "setup_phase_failure"
                            if _setup_flake
                            else "infra_log_patterns"
                        )
                        logger.info(
                            "[RELEASE_QUALIFIER] promotion-CI harvest: v%d CI "
                            "failure is an infrastructure flake (%s) — "
                            "requesting a Strategic delivery decision and "
                            "skipping fix spawn (source %s)",
                            version_number,
                            _flake_reason,
                            ci_source_sha[:8],
                        )
                    # Drift-sentinel path only: the CI run being harvested lives
                    # on the LIVE integration branch (not a torn-down release
                    # branch), so re-running the failed jobs can clear a
                    # transient infra flake for the SAME HEAD.  Without this,
                    # compute_promotion_ci_green_gate stays red for the commit —
                    # it only dispatches a fresh run when NO run exists for the
                    # SHA — and the qualification latch cannot clear until a new
                    # commit lands.  Best-effort: GitHub rejects the rerun on a
                    # run that is still completing or already re-running, and a
                    # rerun that concludes red again still falls through to the
                    # Strategic handoff below.
                    if _flake_budget_remaining > 0:
                        from app.github_checks import rerun_failed_jobs

                        _reran_any = False
                        for _run_id in _ci_result.get("failed_run_ids") or []:
                            if await rerun_failed_jobs(_token, owner, repo, _run_id):
                                _reran_any = True
                        if _reran_any:
                            _flake_outcome = "flake_rerun"
                            logger.info(
                                "[RELEASE_QUALIFIER] promotion-CI harvest: v%d "
                                "infra flake on live integration branch — re-ran "
                                "failed jobs; the green gate can clear for HEAD "
                                "without a new commit",
                                version_number,
                            )
                    # This is an edge-triggered semantic wake, not another
                    # scheduler-owned qualification decision. The marker
                    # coalesces with any already-pending Strategic refresh.
                    try:
                        from app.database import AsyncSessionLocal as _ASL
                        from app.strategic_trigger import request_strategic_refresh

                        async with _ASL() as _rq_session:
                            _refresh_armed = await request_strategic_refresh(
                                _rq_session,
                                project_id,
                                (
                                    "promotion_ci_infra_flake:"
                                    f"v{version_number}:{ci_source_sha[:12]}"
                                ),
                            )
                            if _refresh_armed:
                                await _rq_session.commit()
                            else:
                                await _rq_session.rollback()
                    except Exception:
                        logger.warning(
                            "[RELEASE_QUALIFIER] promotion-CI harvest: v%d "
                            "Strategic refresh request failed; normal cadence "
                            "remains the backstop",
                            version_number,
                            exc_info=True,
                        )
                    return _flake_outcome
        except Exception:
            logger.warning(
                "[RELEASE_QUALIFIER] promotion-CI harvest: infra-flake check "
                "failed for v%d — proceeding to fix spawn",
                version_number,
                exc_info=True,
            )

        logger.info(
            "[RELEASE_QUALIFIER] promotion-CI harvest: spawning auto-remediation "
            "on %s for v%d promotion CI failures (source %s)",
            integration_branch,
            version_number,
            ci_source_sha[:8],
        )
        from app.auto_remediation import _handle_main_branch_ci_failure

        await _handle_main_branch_ci_failure(
            owner=owner,
            repo=repo,
            head_sha=ci_source_sha,
            head_branch=integration_branch,
            project_id=project_id,
            config=auto_remediation_config,
            ci_source_sha=ci_source_sha,
            release_version_id=version_id,
        )
        return "fix_spawned"
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] promotion-CI harvest failed for v%d — the "
            "promotion failure is already recorded; continuing",
            version_number,
        )
        return "error"


async def _finish_managed_check_wait_landing(
    session,
    version: release_version.ReleaseVersion,
    *,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    candidate_sha: str,
    base_record: dict,
    pr_url: str,
    promotion_target: str,
    release_branch: str,
    promoted_sha: str,
    source: str,
) -> bool:
    """Validate and record one authoritative managed promotion landing."""

    alembic_status, alembic_reason = await _validate_migration_chain_post_merge(
        token, owner, repo, pr_number, promoted_sha
    )
    if alembic_status == "defer":
        logger.warning(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: candidate %s "
            "for v%d landed via %s, post-merge Alembic deferred: %s",
            candidate_sha[:8],
            version.version_number,
            source,
            alembic_reason,
        )
        return False
    if alembic_status == "block":
        await _cleanup_managed_check_wait_failure_artifacts(
            token,
            owner,
            repo,
            release_branch,
            version,
            pr_number,
        )
        await _record_promotion_failure(
            session,
            version,
            base_record,
            "migration_guard_blocked",
            detail=alembic_reason,
            pr_url=pr_url,
            lock_released=True,
        )
        await session.commit()
        return True

    logger.info(
        "[RELEASE_QUALIFIER] reconcile managed_check_wait: candidate %s for "
        "v%d is already on %s via %s — recording promoted",
        candidate_sha[:8],
        version.version_number,
        promotion_target,
        source,
    )
    result = await _record_promotion_success(
        session,
        version,
        candidate_sha,
        base_record,
        pr_url,
        token=token,
        owner=owner,
        repo=repo,
        promoted_sha=promoted_sha,
        already_landed=True,
        lock_released=True,
    )
    await session.commit()
    return result.get("status") == "promoted" and not result.get("already_promoted")


async def _record_promotion_integrity_incident(
    session,
    version: release_version.ReleaseVersion,
    base_record: dict,
    *,
    pr_url: str,
    detail: str,
    evidence: dict,
) -> None:
    """Record an unqualified landed PR as a stop-level, non-redrivable incident."""

    version_id = version.id
    await _record_promotion_failure(
        session,
        version,
        base_record,
        "promotion_pr_identity_mismatch",
        detail=(
            f"STOP-LEVEL promotion integrity incident: {detail}. Automatic "
            "promotion redrive is disabled until an operator repairs the target."
        ),
        pr_url=pr_url,
        lock_released=True,
    )
    session.expire_all()
    locked = (
        (
            await session.execute(
                select(release_version.ReleaseVersion)
                .where(release_version.ReleaseVersion.id == version_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if locked is None or locked.status != "open":
        return
    incident = dict(locked.last_qualification or {})
    incident["status"] = "promotion_integrity_violation"
    incident["automatic_redrive_blocked"] = True
    incident["integrity_evidence"] = evidence
    locked.last_qualification = incident
    flag_modified(locked, "last_qualification")
    session.add(locked)


async def _reconcile_if_managed_check_wait(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Reconcile a promotion PR in ``managed_check_wait`` mode.

    For a pending version with ``promotion_pending.managed_check_wait=True``,
    this reconciler periodically checks CI on one exact
    head, re-runs the Alembic guard, revalidates the base/head pair, and submits
    an expected-head merge.

    Resolution paths:

    * **Out-of-band landing** — candidate already reachable from the promotion
      target → record success through the existing promotion success path.
    * **CI still pending / no checks** — leave pending for the next sweep.
    * **CI red** — record ``required_check_failed`` with the failing check
      names so retry machinery applies.
    * **PR closed without merging** — record ``pr_closed_without_merge``.
    * **Alembic guard blocks** — record ``migration_guard_blocked``.
    * **Non-check branch-protection block** (green CI but merge refused) —
      record ``protection_blocked_review``.
    * **Merge conflict** — record ``merge_blocked``.
    * **Merge succeeds** — record through the existing promotion success path.

    Transient GitHub errors leave the version pending (fail-open) so the next
    sweep retries.  Returns True iff the version reached a terminal state.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            version: release_version.ReleaseVersion | None = (
                (
                    await session.execute(
                        select(release_version.ReleaseVersion)
                        .where(release_version.ReleaseVersion.id == version_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                return False
            if version.status != "open":
                return False
            qual = version.last_qualification or {}
            if qual.get("status") != "pending":
                return False
            pending, pending_kind, pending_was_normalized = (
                _canonicalize_promotion_pending(
                    qual.get("promotion_pending"),
                    qualification=qual,
                    version=version,
                )
            )
            if pending_kind == _PROMOTION_PENDING_ABSENT:
                return False
            if pending_kind == _PROMOTION_PENDING_MALFORMED:
                base_record = {
                    k: v for k, v in qual.items() if k != "promotion_pending"
                }
                await _record_promotion_failure(
                    session,
                    version,
                    base_record,
                    "promotion_pending_malformed",
                    detail=(
                        "promotion_pending was neither a persisted promotion PR "
                        "nor a candidate-bound promotion claim"
                    ),
                    lock_released=False,
                )
                await session.commit()
                return True
            if pending is None or pending_kind != _PROMOTION_PENDING_MANAGED:
                return False
            pr_number = pending.get("pr_number")
            if not pr_number:
                return False

            if pending_was_normalized:
                canonical_qual = dict(qual)
                canonical_qual["promotion_pending"] = pending
                version.last_qualification = canonical_qual
                flag_modified(version, "last_qualification")
                session.add(version)
                qual = canonical_qual

            candidate_sha: str | None = qual.get("candidate_sha")
            if not candidate_sha:
                if pending_was_normalized:
                    await session.commit()
                return False

            base_record = {k: v for k, v in qual.items() if k != "promotion_pending"}
            pr_url: str = pending.get("pr_url") or _idempotent_pr_url(base_record)

            # A timed-out PR retains its identity until close succeeds.  The
            # timeout reconciler owns that cleanup debt; never let the ordinary
            # managed waiter merge it while cleanup is pending.
            if pending.get("cleanup_required"):
                if pending_was_normalized:
                    await session.commit()
                return False

            project = await session.get(Project, version.project_id)
            if project is None:
                if pending_was_normalized:
                    await session.commit()
                return False

            integration_branch, promotion_target = _lts_branch_names(project)
            if integration_branch == promotion_target:
                if pending_was_normalized:
                    await session.commit()
                return False

            # Capture the primitives the promotion-CI harvest needs while the ORM
            # rows are still loaded.  The terminal paths below commit (expiring
            # these attributes); reading them afterwards would raise MissingGreenlet
            # under async SQLAlchemy, so snapshot them as plain values now.
            _harvest_project_id = project.id
            _harvest_version_id = version.id
            _harvest_version_number = version.version_number
            # The promotion-CI self-heal gate is resolved from durable,
            # operator-owned build_config (NOT design_doc._auto_remediation, which
            # the strategist rewrites every cycle).  auto_remediation_config is
            # still snapshotted for the SPAWN MECHANICS (rate limits / suspension)
            # and falls back to defaults when the project never customized it.
            _harvest_selfheal_enabled = promotion_ci_selfheal_enabled(project)
            from app.project_telemetry import read_telemetry_or_doc

            _harvest_ar_config = dict(
                await read_telemetry_or_doc(
                    session,
                    _harvest_project_id,
                    project.design_doc,
                    "_auto_remediation",
                    {},
                )
                or {}
            )

            # Release the row lock before GitHub I/O (mirrors the other reconcilers).
            try:
                await session.commit()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: failed to "
                    "release row lock for v%d — skipping",
                    version.version_number,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: rollback "
                        "failed for v%d",
                        version.version_number,
                    )
                return False

            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: no GitHub "
                    "credentials for v%d — leaving pending",
                    version.version_number,
                )
                return False
            token, owner, repo = resolved
            release_branch = f"release-v{version.version_number}"

            # 1. Check an exact out-of-band receipt first — ancestry alone cannot
            #    bind this candidate to a promotion PR or identify its merge SHA.
            landed_receipt = await _resolve_exact_promotion_receipt(
                token,
                owner,
                repo,
                candidate_sha,
                version,
                project=project,
                pr_number=int(pr_number),
            )
            if landed_receipt is not None:
                return await _finish_managed_check_wait_landing(
                    session,
                    version,
                    token=token,
                    owner=owner,
                    repo=repo,
                    pr_number=int(pr_number),
                    candidate_sha=candidate_sha,
                    base_record=base_record,
                    pr_url=pr_url,
                    promotion_target=promotion_target,
                    release_branch=release_branch,
                    promoted_sha=landed_receipt["merge_commit_sha"],
                    source="exact promotion receipt",
                )

            # 2. Inspect the PR state and check runs.
            try:
                pr_data = await github.get_pull_request(
                    token,
                    owner,
                    repo,
                    int(pr_number),
                    project_id=str(version.project_id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: could not "
                    "fetch PR #%s for v%d (%s) — leaving pending",
                    pr_number,
                    version.version_number,
                    exc,
                )
                return False

            if pr_data.get("merged") is True:
                merged_head = pr_data.get("head") or {}
                merged_base = pr_data.get("base") or {}
                merged_head_sha = str(merged_head.get("sha") or "").strip()
                merged_commit_sha = str(
                    pr_data.get("merge_commit_sha") or ""
                ).strip()
                merged_identity_matches = (
                    merged_head_sha == candidate_sha
                    and merged_head.get("ref") == release_branch
                    and merged_base.get("ref") == promotion_target
                    and bool(merged_commit_sha)
                )
                if not merged_identity_matches:
                    detail = (
                        f"merged PR #{pr_number} identity was "
                        f"{merged_base.get('ref')}/{merged_head.get('ref')}@"
                        f"{merged_head_sha[:8] or 'missing'}, expected "
                        f"{promotion_target}/{release_branch}@{candidate_sha[:8]}"
                    )
                    logger.error(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: %s — "
                        "refusing to record an unqualified landing",
                        detail,
                    )
                    await _record_promotion_integrity_incident(
                        session,
                        version,
                        base_record,
                        pr_url=pr_url,
                        detail=detail,
                        evidence={
                            "pr_number": int(pr_number),
                            "pr_url": pr_url,
                            "expected_base_ref": promotion_target,
                            "expected_head_ref": release_branch,
                            "expected_candidate_sha": candidate_sha,
                            "observed_base_ref": merged_base.get("ref"),
                            "observed_head_ref": merged_head.get("ref"),
                            "observed_head_sha": merged_head_sha,
                            "observed_merge_commit_sha": merged_commit_sha,
                        },
                    )
                    await session.commit()
                    return True
                try:
                    merge_reachable = await github.is_ancestor_of(
                        token,
                        owner,
                        repo,
                        merged_commit_sha,
                        promotion_target,
                        project_id=str(version.project_id),
                    )
                except Exception:  # noqa: BLE001 — exact recovery must defer
                    merge_reachable = None
                if merge_reachable is not True:
                    await _persist_managed_check_wait_snapshot(
                        session,
                        version.id,
                        pr_number=int(pr_number),
                        ci_state=_CI_PENDING,
                        snapshot=_managed_check_wait_snapshot(
                            ci_state=_CI_PENDING,
                            classified_at=_utcnow(),
                            head_sha=merged_head_sha,
                            reason="merge_receipt_reachability_unconfirmed",
                            detail=(
                                f"merge commit {merged_commit_sha[:8]} "
                                f"reachability from {promotion_target} is "
                                f"{merge_reachable!r}"
                            ),
                        ),
                    )
                    return False
                return await _finish_managed_check_wait_landing(
                    session,
                    version,
                    token=token,
                    owner=owner,
                    repo=repo,
                    pr_number=int(pr_number),
                    candidate_sha=candidate_sha,
                    base_record=base_record,
                    pr_url=pr_url,
                    promotion_target=promotion_target,
                    release_branch=release_branch,
                    promoted_sha=merged_commit_sha,
                    source="authoritative merged PR",
                )

            if pr_data.get("state") == "closed" and not pr_data.get("merged"):
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                    "for v%d closed without merge — recording pr_closed_without_merge",
                    pr_number,
                    version.version_number,
                )
                await _cleanup_managed_check_wait_failure_artifacts(
                    token,
                    owner,
                    repo,
                    release_branch,
                    version,
                    int(pr_number),
                )
                await _record_promotion_failure(
                    session,
                    version,
                    base_record,
                    "pr_closed_without_merge",
                    detail=f"PR #{pr_number} was closed without merging",
                    pr_url=pr_url,
                    lock_released=True,
                )
                await session.commit()
                # The PR may have been closed while CI was red (v17's case). The
                # remediation handler self-gates: if candidate_sha has no failed
                # checks this no-ops, so harvesting here is safe.
                await _harvest_promotion_ci_failures(
                    project_id=_harvest_project_id,
                    version_id=_harvest_version_id,
                    version_number=_harvest_version_number,
                    owner=owner,
                    repo=repo,
                    ci_source_sha=candidate_sha,
                    integration_branch=integration_branch,
                    auto_remediation_config=_harvest_ar_config,
                    selfheal_enabled=_harvest_selfheal_enabled,
                )
                return True

            if pr_data.get("state") != "open":
                # Unknown/transient state — leave pending.
                classified_at = _utcnow()
                await _persist_managed_check_wait_snapshot(
                    session,
                    version.id,
                    pr_number=int(pr_number),
                    ci_state=_CI_PENDING,
                    snapshot=_managed_check_wait_snapshot(
                        ci_state=_CI_PENDING,
                        classified_at=classified_at,
                        reason="pr_not_open",
                        detail=f"PR state is {pr_data.get('state')!r}",
                    ),
                )
                return False

            pr_head = pr_data.get("head") or {}
            pr_base = pr_data.get("base") or {}
            pr_head_sha: str | None = pr_head.get("sha")
            pr_base_sha: str | None = pr_base.get("sha")
            identity_matches_qualified_candidate = (
                pr_head.get("ref") == release_branch
                and pr_base.get("ref") == promotion_target
                and pr_head_sha == candidate_sha
            )
            if not identity_matches_qualified_candidate:
                detail = (
                    f"observed {pr_base.get('ref')}/{pr_head.get('ref')}@"
                    f"{str(pr_head_sha or '')[:8] or 'missing'}, expected "
                    f"{promotion_target}/{release_branch}@{candidate_sha[:8]}"
                )
                logger.error(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                    "for v%d no longer carries the qualified candidate (%s) — "
                    "leaving pending for branch repair; merge forbidden",
                    pr_number,
                    version.version_number,
                    detail,
                )
                classified_at = _utcnow()
                await _persist_managed_check_wait_snapshot(
                    session,
                    version.id,
                    pr_number=int(pr_number),
                    ci_state=_CI_PENDING,
                    snapshot=_managed_check_wait_snapshot(
                        ci_state=_CI_PENDING,
                        classified_at=classified_at,
                        head_sha=pr_head_sha,
                        reason="qualified_candidate_mismatch",
                        detail=detail,
                    ),
                )
                return False
            if not pr_head_sha:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                    "for v%d has no head SHA — leaving pending",
                    pr_number,
                    version.version_number,
                )
                classified_at = _utcnow()
                await _persist_managed_check_wait_snapshot(
                    session,
                    version.id,
                    pr_number=int(pr_number),
                    ci_state=_CI_PENDING,
                    snapshot=_managed_check_wait_snapshot(
                        ci_state=_CI_PENDING,
                        classified_at=classified_at,
                        reason="missing_pr_head_sha",
                    ),
                )
                return False

            # 3. Check CI status on the PR head.
            try:
                check_result = await github.get_check_runs_cached(
                    token,
                    owner,
                    repo,
                    pr_head_sha,
                    project_id=str(version.project_id),
                    treat_missing_as_success=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: CI fetch "
                    "failed for PR #%s head %s (v%d) (%s) — leaving pending",
                    pr_number,
                    pr_head_sha[:8] if pr_head_sha else "unknown",
                    version.version_number,
                    exc,
                )
                classified_at = _utcnow()
                await _persist_managed_check_wait_snapshot(
                    session,
                    version.id,
                    pr_number=int(pr_number),
                    ci_state=_CI_PENDING,
                    snapshot=_managed_check_wait_snapshot(
                        ci_state=_CI_PENDING,
                        classified_at=classified_at,
                        head_sha=pr_head_sha,
                        reason="ci_fetch_failed",
                        detail=str(exc),
                    ),
                )
                return False

            ci_state = _classify_check_runs(check_result)
            if ci_state == _CI_RED:
                if not _check_result_has_genuine_failure(check_result):
                    # Only transient conclusions (cancelled / timed_out) — do
                    # not tear down the PR or release branch.  Increment the
                    # sweep counter and leave pending until it hits the cap.
                    new_count = await _increment_pending_transient_red_count(
                        session,
                        version.id,
                        pr_number=int(pr_number),
                    )
                    max_sweeps = _max_transient_ci_red_sweeps()
                    if new_count < max_sweeps:
                        logger.warning(
                            "[RELEASE_QUALIFIER] reconcile managed_check_wait: "
                            "PR #%s for v%d has transient-only red CI "
                            "(cancelled/timed_out, sweep %d/%d) — leaving pending",
                            pr_number,
                            version.version_number,
                            new_count,
                            max_sweeps,
                        )
                        return False
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: "
                        "PR #%s for v%d reached transient-red sweep cap (%d) "
                        "— escalating to required_check_failed",
                        pr_number,
                        version.version_number,
                        max_sweeps,
                    )
                blocking_conclusions = {
                    "failure",
                    "cancelled",
                    "timed_out",
                    "action_required",
                }
                failing_names = [
                    d.get("name", "unknown")
                    for d in check_result.get("details", [])
                    if d.get("conclusion") in blocking_conclusions
                ]
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                    "for v%d has red CI — recording required_check_failed: %s",
                    pr_number,
                    version.version_number,
                    failing_names,
                )
                await _cleanup_managed_check_wait_failure_artifacts(
                    token,
                    owner,
                    repo,
                    release_branch,
                    version,
                    int(pr_number),
                )
                await _record_promotion_failure(
                    session,
                    version,
                    base_record,
                    "required_check_failed",
                    detail=(
                        f"PR #{pr_number} required checks failed: "
                        f"{', '.join(failing_names[:10]) or 'unknown'}"
                    ),
                    pr_url=pr_url,
                    lock_released=True,
                )
                await session.commit()
                # Harvest the failing tests into a fix task on the integration
                # branch so the next qualification doesn't re-promote the same
                # red code (promotion-gate loop). Best-effort; never raises.
                await _harvest_promotion_ci_failures(
                    project_id=_harvest_project_id,
                    version_id=_harvest_version_id,
                    version_number=_harvest_version_number,
                    owner=owner,
                    repo=repo,
                    ci_source_sha=candidate_sha,
                    integration_branch=integration_branch,
                    auto_remediation_config=_harvest_ar_config,
                    selfheal_enabled=_harvest_selfheal_enabled,
                )
                return True

            if ci_state != _CI_GREEN:
                # For a no-CI repo, a definitive no_checks result means CI will
                # never run — treat it as "proceed" and fall through to the merge
                # step rather than leaving the version stranded waiting on checks
                # that cannot run.
                #
                # Base-aware: gate on proceed_on_no_checks(project,
                # promotion_target) rather than the bare project-wide
                # requires_ci_checks. The promotion PR targets ``promotion_target``
                # (``main``), which is a ci_required_branches member by default —
                # so a no_checks there is NEVER treated as proceed, even when the
                # project relaxed requires_ci_checks for its integration branch.
                # This closes the race where the promotion PR's empty-checks window
                # (PR open, GitHub hasn't posted check-runs yet) could otherwise
                # merge to main untested.
                from app.ci_resolution import proceed_on_no_checks

                if ci_state == _CI_NO_CHECKS and proceed_on_no_checks(
                    project, promotion_target
                ):
                    logger.info(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                        "for v%d reports no_checks on a no-CI repo — proceeding "
                        "to merge attempt",
                        pr_number,
                        version.version_number,
                    )
                else:
                    # Checks pending or no-checks on a CI-gated repo — leave
                    # pending for the next sweep.
                    classified_at = _utcnow()
                    from_api_error = bool(
                        isinstance(check_result, dict)
                        and check_result.get("_from_api_error")
                    )
                    # Compute api_error_sweep_count from the pre-lock read; this
                    # value is written verbatim into the snapshot by
                    # _persist_managed_check_wait_snapshot (no re-derive under lock).
                    stale_pending = (version.last_qualification or {}).get(
                        "promotion_pending"
                    ) or {}
                    old_api_error_count = int(
                        stale_pending.get("api_error_sweep_count") or 0
                    )
                    api_error_sweep_count = (
                        old_api_error_count + 1 if from_api_error else 0
                    )
                    check_details = (
                        check_result.get("details", [])
                        if isinstance(check_result, dict)
                        else []
                    )
                    check_summary = [
                        {
                            k: d.get(k)
                            for k in ("name", "status", "conclusion")
                            if d.get(k) is not None
                        }
                        for d in check_details[
                            :_MANAGED_CHECK_WAIT_SNAPSHOT_DETAIL_LIMIT
                        ]
                        if isinstance(d, dict)
                    ]
                    if (
                        from_api_error
                        and api_error_sweep_count >= _API_ERROR_SWEEP_WARN_THRESHOLD
                    ):
                        logger.warning(
                            "[RELEASE_QUALIFIER] reconcile managed_check_wait: "
                            "PR #%s for v%d has %d consecutive GitHub API-error "
                            "sweeps — GitHub may be degraded; cannot distinguish "
                            "api-error pending from genuine in-progress CI. "
                            "head_sha=%s",
                            pr_number,
                            version.version_number,
                            api_error_sweep_count,
                            pr_head_sha[:8] if pr_head_sha else "unknown",
                        )
                    else:
                        logger.info(
                            "[RELEASE_QUALIFIER] reconcile managed_check_wait: "
                            "PR #%s for v%d leaving pending — ci_state=%s, "
                            "head_sha=%s, api_error=%s, check_count=%d, checks=%s",
                            pr_number,
                            version.version_number,
                            ci_state,
                            pr_head_sha[:8] if pr_head_sha else "unknown",
                            from_api_error,
                            len(check_details),
                            check_summary,
                        )
                    await _persist_managed_check_wait_snapshot(
                        session,
                        version.id,
                        pr_number=int(pr_number),
                        ci_state=ci_state,
                        snapshot=_managed_check_wait_snapshot(
                            ci_state=ci_state,
                            classified_at=classified_at,
                            head_sha=pr_head_sha,
                            check_result=check_result,
                            from_api_error=from_api_error,
                            api_error_sweep_count=api_error_sweep_count,
                        ),
                    )
                    return False

            # 4. CI is green (or no_checks on a no-CI repo) — re-run the Alembic dual-head guard before merging.
            # Use the 3-way verdict so 'defer' (metadata temporarily unavailable)
            # leaves the version pending for the next sweep without cleanup, while
            # 'block' (genuine collision) records the terminal failure.
            from app.rollup_migration_validation import _validate_rollup_migration_chain

            migration_verdict, hold_reason = await _validate_rollup_migration_chain(
                token, owner, repo, int(pr_number), target_branch=promotion_target
            )
            if migration_verdict == "defer":
                logger.info(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: migration "
                    "chain guard DEFERRED for PR #%s (v%d) — leaving pending for "
                    "next sweep. Reason: %s",
                    pr_number,
                    version.version_number,
                    hold_reason,
                )
                return False
            if migration_verdict == "block":
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: Alembic "
                    "guard BLOCKED for PR #%s (v%d): %s — recording "
                    "migration_guard_blocked",
                    pr_number,
                    version.version_number,
                    hold_reason,
                )
                await _cleanup_managed_check_wait_failure_artifacts(
                    token,
                    owner,
                    repo,
                    release_branch,
                    version,
                    int(pr_number),
                )
                await _record_promotion_failure(
                    session,
                    version,
                    base_record,
                    "migration_guard_blocked",
                    detail=hold_reason,
                    pr_url=pr_url,
                    lock_released=True,
                )
                await session.commit()
                return True

            # CI and the migration guard authorised the exact target observed
            # above.  Serialize final submissions to this repository/base and
            # PR with the canonical review/fix lock order, then re-read the live
            # pair inside both locks immediately before the expected-head PUT.
            # If either side advanced, leave the version pending so the next
            # sweep re-runs all evidence against the new pair.
            target_checked_at = _utcnow()
            initial_target_valid = bool(
                pr_head_sha
                and pr_base_sha
                and pr_head.get("ref") == release_branch
                and pr_base.get("ref") == promotion_target
                and pr_head_sha == candidate_sha
            )
            from app.task_locking import acquire_merge_submission_locks

            await acquire_merge_submission_locks(
                session,
                project_id=version.project_id,
                repo_identity=f"{owner}/{repo}",
                base_branch=promotion_target,
                pr_number=int(pr_number),
            )
            try:
                fresh_pr_data = await github.get_pull_request(
                    token,
                    owner,
                    repo,
                    int(pr_number),
                    project_id=str(version.project_id),
                )
            except Exception as exc:  # noqa: BLE001 — retry next sweep
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: could "
                    "not re-read PR #%s target after checks for v%d (%s) — "
                    "leaving pending",
                    pr_number,
                    version.version_number,
                    exc,
                )
                await _persist_managed_check_wait_snapshot(
                    session,
                    version.id,
                    pr_number=int(pr_number),
                    ci_state=ci_state,
                    snapshot=_managed_check_wait_snapshot(
                        ci_state=ci_state,
                        classified_at=target_checked_at,
                        head_sha=pr_head_sha,
                        check_result=check_result,
                        reason="pr_target_refetch_failed",
                        detail=str(exc),
                    ),
                )
                return False

            fresh_head = fresh_pr_data.get("head") or {}
            fresh_base = fresh_pr_data.get("base") or {}
            fresh_head_sha = str(fresh_head.get("sha") or "").strip()
            fresh_base_sha = str(fresh_base.get("sha") or "").strip()
            target_changed = (
                not initial_target_valid
                or fresh_pr_data.get("state") != "open"
                or fresh_head.get("ref") != release_branch
                or fresh_base.get("ref") != promotion_target
                or fresh_head_sha != candidate_sha
                or fresh_head_sha != pr_head_sha
                or fresh_base_sha != pr_base_sha
            )
            if target_changed:
                detail = (
                    f"{pr_base.get('ref')}@{str(pr_base_sha or '')[:8]}/"
                    f"{pr_head.get('ref')}@{str(pr_head_sha or '')[:8]} -> "
                    f"{fresh_base.get('ref')}@{fresh_base_sha[:8]}/"
                    f"{fresh_head.get('ref')}@{fresh_head_sha[:8]} "
                    f"state={fresh_pr_data.get('state')}"
                )
                logger.info(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                    "target changed while checks ran for v%d (%s) — leaving "
                    "pending for exact-target revalidation",
                    pr_number,
                    version.version_number,
                    detail,
                )
                await _persist_managed_check_wait_snapshot(
                    session,
                    version.id,
                    pr_number=int(pr_number),
                    ci_state=ci_state,
                    snapshot=_managed_check_wait_snapshot(
                        ci_state=ci_state,
                        classified_at=target_checked_at,
                        head_sha=pr_head_sha,
                        check_result=check_result,
                        reason="pr_target_changed_after_checks",
                        detail=detail,
                    ),
                )
                return False

            # 5. Attempt the merge.
            try:
                merge_result = await github.merge_pull_request(
                    token,
                    owner,
                    repo,
                    int(pr_number),
                    merge_method="merge",
                    project_id=str(version.project_id),
                    expected_head_sha=pr_head_sha,
                )
            except Exception as exc:  # noqa: BLE001 — transient error, leave pending
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: merge "
                    "raised for PR #%s (v%d): %s — leaving pending",
                    pr_number,
                    version.version_number,
                    exc,
                )
                return False

            if not merge_result.get("merged"):
                # Disambiguate: the PR may have already merged (idempotency).
                retry_receipt = await _resolve_exact_promotion_receipt(
                    token,
                    owner,
                    repo,
                    candidate_sha,
                    version,
                    project=project,
                    pr_number=int(pr_number),
                )
                if retry_receipt is not None:
                    logger.info(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                        "for v%d returned merged=False but is in fact merged — "
                        "recording success (idempotent)",
                        pr_number,
                        version.version_number,
                    )
                    # Fall through to the success block below.
                    merge_result = {
                        "merged": True,
                        "sha": retry_receipt["merge_commit_sha"],
                    }
                elif refusal := promotion_merge_refusal(merge_result, owner, repo):
                    logger.warning(
                        f"[RELEASE_QUALIFIER] reconcile managed_check_wait: PR "
                        f"#{pr_number} for v{version.version_number} did not "
                        f"merge (permanent) — recording {refusal['error']}"
                    )
                    await _cleanup_managed_check_wait_failure_artifacts(
                        token,
                        owner,
                        repo,
                        release_branch,
                        version,
                        int(pr_number),
                    )
                    await _record_promotion_failure(
                        session,
                        version,
                        base_record,
                        refusal["error"],
                        detail=refusal["detail"],
                        pr_url=pr_url,
                        conflict=refusal["conflict"],
                        lock_released=True,
                    )
                    await session.commit()
                    return True
                else:
                    # Green CI but merge refused — inspect the 405 message to
                    # determine if this is a terminal condition (PR closed, auto-
                    # merge disabled, protection config changed) or a retriable
                    # one (required checks not yet passed on GitHub's side).
                    # Terminal 405s are recorded immediately with a distinct
                    # reason; retriable/unknown ones fall through to the CI
                    # re-check to confirm before committing to a failure.
                    msg = merge_result.get("message", "merge not completed")
                    cause_405 = _classify_405_message(msg)
                    if cause_405 == "terminal":
                        logger.warning(
                            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                            "for v%d merge refused with terminal 405 reason — "
                            "recording merge_refused_405_terminal: %s",
                            pr_number,
                            version.version_number,
                            msg,
                        )
                        await _cleanup_managed_check_wait_failure_artifacts(
                            token,
                            owner,
                            repo,
                            release_branch,
                            version,
                            int(pr_number),
                        )
                        await _record_promotion_failure(
                            session,
                            version,
                            base_record,
                            "merge_refused_405_terminal",
                            detail=str(msg)[:300],
                            pr_url=pr_url,
                            lock_released=True,
                        )
                        await session.commit()
                        return True
                    if cause_405 == "retriable":
                        # GitHub reports required checks not yet passed — CI is
                        # still pending on GitHub's side despite our local green
                        # classification.  Leave the version pending so the next
                        # sweep retries once GitHub's required checks settle.
                        logger.info(
                            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                            "for v%d merge refused with retriable 405 (required checks "
                            "not yet passed on GitHub) — leaving pending: %s",
                            pr_number,
                            version.version_number,
                            msg,
                        )
                        classified_at = _utcnow()
                        await _persist_managed_check_wait_snapshot(
                            session,
                            version.id,
                            pr_number=int(pr_number),
                            ci_state=_CI_PENDING,
                            snapshot=_managed_check_wait_snapshot(
                                ci_state=_CI_PENDING,
                                classified_at=classified_at,
                                head_sha=pr_head_sha,
                                reason="merge_refused_405_retriable",
                                detail=str(msg)[:200],
                            ),
                        )
                        return False
                    try:
                        recheck_result = await github.get_check_runs_cached(
                            token,
                            owner,
                            repo,
                            pr_head_sha,
                            project_id=str(version.project_id),
                            treat_missing_as_success=False,
                        )
                        recheck_state = _classify_check_runs(recheck_result)
                    except Exception as exc:  # noqa: BLE001 — fail-open, leave pending
                        logger.warning(
                            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                            "for v%d merge refused (%s); CI re-check fetch failed (%s) "
                            "— leaving pending for next sweep",
                            pr_number,
                            version.version_number,
                            msg,
                            exc,
                        )
                        classified_at = _utcnow()
                        await _persist_managed_check_wait_snapshot(
                            session,
                            version.id,
                            pr_number=int(pr_number),
                            ci_state=_CI_PENDING,
                            snapshot=_managed_check_wait_snapshot(
                                ci_state=_CI_PENDING,
                                classified_at=classified_at,
                                head_sha=pr_head_sha,
                                reason="merge_refused_recheck_failed",
                                detail=str(msg)[:200],
                            ),
                        )
                        return False

                    if recheck_state != _CI_GREEN:
                        # CI flipped back — transient race.  Leave pending so the
                        # next sweep retries once CI settles again.
                        logger.info(
                            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                            "for v%d merge refused (%s) and CI re-check is %s — "
                            "leaving pending (transient race)",
                            pr_number,
                            version.version_number,
                            msg,
                            recheck_state,
                        )
                        classified_at = _utcnow()
                        await _persist_managed_check_wait_snapshot(
                            session,
                            version.id,
                            pr_number=int(pr_number),
                            ci_state=recheck_state,
                            snapshot=_managed_check_wait_snapshot(
                                ci_state=recheck_state,
                                classified_at=classified_at,
                                head_sha=pr_head_sha,
                                check_result=recheck_result,
                                reason="merge_refused_ci_not_green",
                                detail=str(msg)[:200],
                            ),
                        )
                        return False

                    # CI still green on re-check — the refusal is genuinely a
                    # branch-protection / required-reviews block that a retry will
                    # not resolve.  Record terminal failure.
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                        "for v%d merge refused despite green CI (re-check confirms "
                        "green): %s — recording protection_blocked_review",
                        pr_number,
                        version.version_number,
                        msg,
                    )
                    await _cleanup_managed_check_wait_failure_artifacts(
                        token,
                        owner,
                        repo,
                        release_branch,
                        version,
                        int(pr_number),
                    )
                    await _record_promotion_failure(
                        session,
                        version,
                        base_record,
                        "protection_blocked_review",
                        detail=str(msg)[:300],
                        pr_url=pr_url,
                        lock_released=True,
                    )
                    await session.commit()
                    return True

            # 6. Merge succeeded — record through the existing success path.
            logger.info(
                "[RELEASE_QUALIFIER] reconcile managed_check_wait: merge succeeded "
                "for PR #%s (v%d) — recording promotion",
                pr_number,
                version.version_number,
            )
            await _delete_release_branch_best_effort(
                token, owner, repo, release_branch, version
            )
            promoted_sha_new: str | None = (
                str(merge_result.get("sha") or "").strip() or None
            )
            if promoted_sha_new is None:
                success_receipt = await _resolve_exact_promotion_receipt(
                    token,
                    owner,
                    repo,
                    candidate_sha,
                    version,
                    project=project,
                    pr_number=int(pr_number),
                )
                if success_receipt is None:
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: merge "
                        "succeeded for PR #%s but no exact merge receipt is "
                        "readable yet — retaining managed wait",
                        pr_number,
                    )
                    return False
                promoted_sha_new = success_receipt["merge_commit_sha"]
            result = await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                pr_url,
                token=token,
                owner=owner,
                repo=repo,
                promoted_sha=promoted_sha_new or candidate_sha,
                lock_released=True,
            )
            await session.commit()
            return result.get("status") == "promoted" and not result.get(
                "already_promoted"
            )
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile managed_check_wait: unexpected "
                "error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: rollback "
                    "failed for version %s",
                    version_id,
                )
            return False


async def _cleanup_managed_check_wait_failure_artifacts(
    token: str,
    owner: str,
    repo: str,
    release_branch: str,
    version: release_version.ReleaseVersion,
    pr_number: int,
) -> bool:
    """Close the managed promotion PR and best-effort delete its branch.

    The return value is authoritative only for PR closure.  Branch deletion is
    cosmetic after a terminal close, but callers must retain cleanup debt when
    the PR itself could still be open or they could redrive a duplicate.
    """
    closed = False
    try:
        closed = bool(
            await github.close_pull_request(
                token,
                owner,
                repo,
                pr_number,
                project_id=str(version.project_id),
            )
        )
        if not closed:
            try:
                pr_state = await github.get_pull_request(
                    token,
                    owner,
                    repo,
                    pr_number,
                    project_id=str(version.project_id),
                )
                closed = (
                    pr_state.get("state") == "closed"
                    and pr_state.get("merged") is not True
                )
            except Exception:  # noqa: BLE001 — retain debt when state is unknown
                closed = False
            if not closed:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: close of "
                    "PR #%s for v%d was not acknowledged and terminal state "
                    "could not be confirmed — retaining cleanup debt",
                    pr_number,
                    version.version_number,
                )
    except Exception as exc:  # noqa: BLE001 — caller decides whether debt may clear
        logger.warning(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: could not close "
            "PR #%s for v%d (%s) — continuing with branch cleanup",
            pr_number,
            version.version_number,
            exc,
        )
    await _delete_release_branch_best_effort(
        token, owner, repo, release_branch, version
    )
    return closed


async def _reconcile_if_pending_qualification_superseded(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Reconcile an open version whose pending qualification candidate landed out-of-band.

    When an operator force-promotes a candidate to the promotion target without
    going through the normal qualification -> managed promotion
    path, the version is left with ``last_qualification.status == "pending"``
    and NO ``promotion_pending`` sub-key.  The 6h qualification_timeout backstop
    eventually fires, but stamps ``blocker='qualification_timeout'`` — wrong
    provenance that loses the out-of-band landing signal.

    This reconciler detects that case promptly: for a ``pending`` qualification
    with no ``promotion_pending`` sub-key, it checks whether the candidate is
    already reachable from the promotion target.  If it is, it records
    ``not_promoted/qualification_superseded`` (preserving ``candidate_sha`` and
    any carried audit findings so the re-qualify steering and finding ledger can
    act on them) and returns the version to stabilize immediately.

    Fail-closed: a GitHub credential/API failure leaves the version untouched so
    the normal qualification-timeout backstop still fires — we never fabricate a
    supersession verdict.  Returns True iff the version was reconciled.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            version: release_version.ReleaseVersion | None = (
                (
                    await session.execute(
                        select(release_version.ReleaseVersion)
                        .where(release_version.ReleaseVersion.id == version_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                return False
            if version.status != "open":
                return False
            qual = version.last_qualification or {}
            if qual.get("status") != "pending":
                return False
            # promotion_pending means the qualifier itself initiated a promotion
            # PR — that path is owned by _reconcile_if_managed_check_wait and
            # _reconcile_if_promotion_pending_timed_out.
            if qual.get("promotion_pending"):
                return False
            candidate_sha: str | None = qual.get("candidate_sha")
            if not candidate_sha:
                return False

            project = await session.get(Project, version.project_id)
            if project is None:
                return False

            # Single-branch repo guard — mirrors promote().  When the integration
            # branch IS the promotion target there is no separate release trunk,
            # so _candidate_already_on_main would always be True and this
            # reconciler would spuriously supersede every pending qualification.
            # Bail out inert, mirroring the other reconcilers' "lts_inactive" skip.
            integration_branch, promotion_target = _lts_branch_names(project)
            if integration_branch == promotion_target:
                logger.debug(
                    "[RELEASE_QUALIFIER] reconcile pending_superseded: v%d "
                    "integration branch (%s) is the promotion target — "
                    "single-branch repo, LTS inactive; nothing to reconcile",
                    version.version_number,
                    promotion_target,
                )
                return False

            # In-flight qualification guard.  While required children are still
            # non-terminal the normal completion path
            # (_required_children_terminal → _finalize_qualification) will fire
            # once they finish — no need for the superseded reconciler to race
            # ahead or mis-classify an in-progress qualification as superseded.
            # Only proceed when children are absent OR have reached a terminal
            # state without the normal finalizer running (the stuck-qualification
            # scenario this reconciler exists to resolve).
            #
            # EVERY required qualifier child must gate this, not just audit +
            # smoketest: a still-running blind merge-grade or audit-challenge
            # child is just as much "in flight" as a running audit.  The prior
            # older partial-child check diverged from the canonical in-flight
            # definition (_qualification_in_flight, which also covers merge_grade
            # and challenge) — so an attempt whose audit had finished but
            # whose merge-grade/challenge was still running could be falsely
            # superseded, cancelling a legitimately in-flight attempt and
            # burning it, exactly when automain advances beneath the pinned
            # candidate.  Checking all required children keeps the pinned attempt
            # running to a verdict on its snapshotted candidate_sha.
            #
            # Semantics preserved: a child id that is absent/unparseable is NOT
            # treated as in-flight here (mirrors the out-of-band force-promotion
            # incident, whose pending record tracks no children) — the reconciler
            # still supersedes those.  Only a child row that EXISTS and is
            # non-terminal holds off supersession.
            _TERMINAL = frozenset(
                {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.BLOCKED,
                }
            )
            # Qualifier VM smoke is retired; legacy smoke ids never delay or
            # protect a pending qualification from reconciliation.
            in_flight_child_keys = ["audit_task_id"]
            # The blind merge-grade and audit-challenge children are required
            # only when the qualifying run recorded them (a non-empty goal /
            # verified findings); absent ids simply skip.
            if qual.get("merge_grade_task_id"):
                in_flight_child_keys.append("merge_grade_task_id")
            if qual.get("challenge_task_id"):
                in_flight_child_keys.append("challenge_task_id")
            for _child_key in in_flight_child_keys:
                _child_raw = qual.get(_child_key)
                if not _child_raw:
                    continue
                try:
                    _child_uuid = uuid.UUID(str(_child_raw))
                except (ValueError, TypeError):
                    continue
                _child_t = await session.get(Task, _child_uuid)
                if _child_t is not None and _child_t.status not in _TERMINAL:
                    logger.debug(
                        "[RELEASE_QUALIFIER] reconcile pending_superseded: v%d "
                        "%s still non-terminal (%s) — deferring to the normal "
                        "finalize path so the attempt runs to a verdict on its "
                        "pinned candidate",
                        version.version_number,
                        _child_key,
                        _child_t.status,
                    )
                    return False

            # Release the row lock before GitHub I/O (mirrors the other reconcilers).
            try:
                await session.commit()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile pending_superseded: failed to "
                    "release row lock for v%d before GitHub I/O — skipping",
                    version.version_number,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] reconcile pending_superseded: rollback "
                        "failed for v%d after commit error",
                        version.version_number,
                    )
                return False

            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile pending_superseded: no GitHub "
                    "credentials for v%d — skipping (fail-closed)",
                    version.version_number,
                )
                return False
            token, owner, repo = resolved

            if not await _candidate_already_on_main(
                token, owner, repo, candidate_sha, version, project=project
            ):
                return False

            logger.info(
                "[RELEASE_QUALIFIER] reconcile pending_superseded: candidate "
                "%s for v%d is on %s without a promotion_pending marker — "
                "force-promoted out of band; recording qualification_superseded",
                candidate_sha[:8],
                version.version_number,
                promotion_target,
            )

            # Write supersession directly — mirroring _force_finalize_timed_out_qualification.
            # Preserve all existing qual fields (candidate_sha, audit findings, etc.)
            # so the re-qualify steering and finding ledger can act on them.
            qual = dict(version.last_qualification or {})
            record = {
                **qual,
                "status": "not_promoted",
                "blocker": "qualification_superseded",
                "detail": (
                    f"candidate {candidate_sha[:12]} is already reachable from "
                    f"{promotion_target} without a qualifier-initiated promotion — "
                    f"force-promoted out of band; returning to stabilize"
                ),
                "finalized_at": _utcnow().isoformat(),
            }
            version.last_qualification = record
            flag_modified(version, "last_qualification")
            release_version.set_version_phase(
                version,
                "stabilize",
                reasoning=(
                    f"qualification v{version.version_number} superseded out of band "
                    f"(candidate {candidate_sha[:8]} on {promotion_target} without "
                    f"promotion_pending) — returning to stabilize"
                ),
            )
            session.add(version)
            await session.commit()
            return True
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile pending_superseded: unexpected "
                "error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile pending_superseded: rollback "
                    "failed for version %s after unexpected error",
                    version_id,
                )
            return False


async def _reconcile_if_promotion_pending_timed_out(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Force-fail a ``promotion_pending`` sub-state that has stranded too long.

    Audit C9: the replacement backstop for the ``promotion_pending`` sub-state,
    which intentionally bypasses the 6h qualification timeout (see the module
    comment on ``_PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT``).

    For an ``open`` version with ``last_qualification.status == "pending"`` and a
    ``promotion_pending`` sub-state whose ``enabled_at`` age exceeds the
    configurable cap, first persists ``cleanup_required`` without clearing the
    PR identity.  GitHub is then authoritative: an exact merged receipt records
    promotion success; a closed-unmerged PR (or an open PR successfully closed)
    records ``promotion_pending_timeout`` and clears the sub-state; a wrong
    merged identity records a stop-level integrity incident; and unknown state
    or failed closure retains cleanup debt for a later sweep.  Returns True iff
    a terminal success/failure/integrity result was recorded.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            version: release_version.ReleaseVersion | None = (
                (
                    await session.execute(
                        select(release_version.ReleaseVersion)
                        .where(release_version.ReleaseVersion.id == version_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if version is None:
                return False
            if version.status != "open":
                return False
            qual = version.last_qualification or {}
            if qual.get("status") != "pending":
                return False
            raw_pending = qual.get("promotion_pending")
            pending, pending_kind, pending_was_normalized = (
                _canonicalize_promotion_pending(
                    raw_pending,
                    qualification=qual,
                    version=version,
                )
            )
            if pending_kind == _PROMOTION_PENDING_ABSENT:
                return False
            base_record = {k: v for k, v in qual.items() if k != "promotion_pending"}
            if pending_kind == _PROMOTION_PENDING_MALFORMED:
                await _record_promotion_failure(
                    session,
                    version,
                    base_record,
                    "promotion_pending_malformed",
                    detail=(
                        "promotion_pending was neither a persisted promotion PR "
                        "nor a candidate-bound promotion claim"
                    ),
                    lock_released=False,
                )
                await session.commit()
                return True
            if pending is None:
                return False
            if pending_was_normalized:
                qual = dict(qual)
                qual["promotion_pending"] = pending
                version.last_qualification = qual
                flag_modified(version, "last_qualification")
                session.add(version)

            # Canonicalization guarantees a parseable clock for both valid
            # shapes.  Keep this defensive fallback total for hand-built rows.
            enabled_at = _parse_promotion_pending_time(pending.get("enabled_at"))
            if enabled_at is None:
                enabled_at = _promotion_pending_fallback_enabled_at(qual, version)

            cap_seconds = _promotion_pending_timeout_seconds()
            # A bare promotion CLAIM (``claiming`` + no ``pr_number``) is only
            # ever alive for promote()'s 30-120s I/O window; if it has aged past
            # the short claim bound, promote() crashed between claiming and
            # opening the PR, so GC it now instead of letting it inherit the 24h
            # cap and wedge the version out of re-qualification.
            _is_bare_claim = pending_kind == _PROMOTION_PENDING_CLAIM
            if _is_bare_claim:
                # A bare claim is promote()'s crashed I/O window, not a promotion
                # waiting on pipeline progress — it will NOT self-heal on resume,
                # so GC it on RAW age even while paused.
                cap_seconds = min(cap_seconds, _PROMOTION_CLAIM_TIMEOUT_SECONDS)
                age_seconds = (_utcnow() - enabled_at).total_seconds()
            else:
                # PAUSE-AWARE: an armed promotion waiting on CI/merge makes no
                # progress while the pipeline is PAUSED. Counting pause time toward
                # the 24h cap would knock the version back to stabilize and re-fire
                # the whole qualify→promote triple for the pause's entire duration
                # (the qualifier churn observed while paused). Subtract pause time
                # via the shared pause_clock (the promotion_pending dict is armed at
                # several sites with no cleanly-stampable baseline, so the open pause
                # window is excluded; an optional ``pause_baseline``, if a future arm
                # site stamps one, upgrades this to exact across closed windows).
                # deploy_state=None → raw (unchanged legacy/test path).
                _deploy_state = await session.get(DeploymentState, 1)
                age_seconds = pause_clock.active_elapsed_seconds(
                    _deploy_state, enabled_at, pending.get("pause_baseline")
                )
            if age_seconds < cap_seconds:
                if pending_was_normalized:
                    await session.commit()
                return False

            age_hours = age_seconds / 3600.0
            pr_number = pending.get("pr_number")
            pr_url: str | None = pending.get("pr_url")
            if _is_bare_claim:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile promotion_claim_abandoned: v%d "
                    "held a bare promotion claim %.0fs (> %.0fs cap) with no PR "
                    "recorded — promote() crashed somewhere in its promotion I/O "
                    "(branch/PR/merge) before recording a terminal state; clearing "
                    "the claim so the version returns to stabilize and re-qualifies",
                    version.version_number,
                    age_seconds,
                    cap_seconds,
                )
                _timeout_error = "promotion_claim_abandoned"
                _timeout_detail = (
                    f"bare promotion claim stranded {age_seconds:.0f}s "
                    f"(> {cap_seconds:.0f}s cap) with no PR recorded — promote() "
                    f"crashed mid-I/O before a terminal state; cleared so the "
                    f"version retries qualification"
                )
            else:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile promotion_pending_timeout: v%d "
                    "promotion PR #%s has been pending %.1fh (> %.1fh cap) — "
                    "force-failing as promotion_pending_timeout so the version "
                    "returns to stabilize and the normal backstop re-arms",
                    version.version_number,
                    pr_number,
                    age_hours,
                    cap_seconds / 3600.0,
                )
                _timeout_error = "promotion_pending_timeout"
                _timeout_detail = (
                    f"promotion PR #{pr_number} stranded in promotion_pending "
                    f"for {age_hours:.0f}h (> {cap_seconds / 3600.0:.0f}h cap) "
                    f"without a terminal merge/close/CI signal — force-failed so "
                    f"the version retries qualification"
                )

            if _is_bare_claim:
                await _record_promotion_failure(
                    session,
                    version,
                    base_record,
                    _timeout_error,
                    detail=_timeout_detail,
                    pr_url=pr_url,
                    lock_released=False,
                )
                await session.commit()
                return True

            # A persisted PR must reach a confirmed terminal state before its
            # identity is removed.  Otherwise a close/API failure followed by a
            # restart lets the redriver open a second promotion PR while the
            # first is still live.  Persist the cleanup debt first; the ordinary
            # managed reconciler refuses to merge cleanup-required rows and every
            # later timeout sweep resumes this exact PR.
            cleanup_pending = dict(pending)
            cleanup_pending["cleanup_required"] = True
            cleanup_pending["timeout_error"] = _timeout_error
            cleanup_pending["timeout_detail"] = _timeout_detail
            cleanup_qual = dict(qual)
            cleanup_qual["promotion_pending"] = cleanup_pending
            version.last_qualification = cleanup_qual
            flag_modified(version, "last_qualification")
            session.add(version)
            await session.commit()

            _ver = await session.get(release_version.ReleaseVersion, version_id)
            _proj = (
                await session.get(Project, _ver.project_id)
                if _ver is not None
                else None
            )
            if _ver is None or _proj is None:
                return False
            _resolved = await _resolve_github_with_session(session, _proj)
            if _resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile promotion_pending_timeout: "
                    "PR #%s cleanup still requires GitHub credentials — debt "
                    "retained; requalification remains fenced",
                    pr_number,
                )
                return False
            _token, _owner, _repo = _resolved
            _integration_branch, _promotion_target = _lts_branch_names(_proj)
            _release_branch = f"release-v{_ver.version_number}"
            try:
                current_pr = await github.get_pull_request(
                    _token,
                    _owner,
                    _repo,
                    int(pr_number),
                    project_id=str(_ver.project_id),
                )
            except Exception as exc:  # noqa: BLE001 — unknown state retains debt
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile promotion_pending_timeout: "
                    "could not classify PR #%s before cleanup (%s) — retaining "
                    "debt",
                    pr_number,
                    exc,
                )
                return False

            if current_pr.get("merged") is True:
                current_head = current_pr.get("head") or {}
                current_base = current_pr.get("base") or {}
                current_merge_sha = str(
                    current_pr.get("merge_commit_sha") or ""
                ).strip()
                expected_candidate = str(cleanup_qual.get("candidate_sha") or "")
                if not (
                    current_head.get("ref") == _release_branch
                    and current_head.get("sha") == expected_candidate
                    and current_base.get("ref") == _promotion_target
                    and current_merge_sha
                ):
                    detail = (
                        f"timed-out PR #{pr_number} merged as "
                        f"{current_base.get('ref')}/{current_head.get('ref')}@"
                        f"{str(current_head.get('sha') or '')[:8] or 'missing'}, "
                        f"expected {_promotion_target}/{_release_branch}@"
                        f"{expected_candidate[:8]}"
                    )
                    await _record_promotion_integrity_incident(
                        session,
                        _ver,
                        base_record,
                        pr_url=str(pr_url or ""),
                        detail=detail,
                        evidence={
                            "pr_number": int(pr_number),
                            "pr_url": pr_url,
                            "expected_base_ref": _promotion_target,
                            "expected_head_ref": _release_branch,
                            "expected_candidate_sha": expected_candidate,
                            "observed_base_ref": current_base.get("ref"),
                            "observed_head_ref": current_head.get("ref"),
                            "observed_head_sha": current_head.get("sha"),
                            "observed_merge_commit_sha": current_merge_sha,
                        },
                    )
                    await session.commit()
                    return True
                receipt = await _resolve_exact_promotion_receipt(
                    _token,
                    _owner,
                    _repo,
                    expected_candidate,
                    _ver,
                    project=_proj,
                    pr_number=int(pr_number),
                )
                if receipt is None:
                    return False
                return await _finish_managed_check_wait_landing(
                    session,
                    _ver,
                    token=_token,
                    owner=_owner,
                    repo=_repo,
                    pr_number=int(pr_number),
                    candidate_sha=expected_candidate,
                    base_record=base_record,
                    pr_url=receipt.get("pr_url") or str(pr_url or ""),
                    promotion_target=_promotion_target,
                    release_branch=_release_branch,
                    promoted_sha=receipt["merge_commit_sha"],
                    source="timeout cleanup race",
                )

            if (
                current_pr.get("state") == "closed"
                and current_pr.get("merged") is False
            ):
                await _delete_release_branch_best_effort(
                    _token, _owner, _repo, _release_branch, _ver
                )
                cleanup_confirmed = True
            elif current_pr.get("state") == "open":
                cleanup_confirmed = (
                    await _cleanup_managed_check_wait_failure_artifacts(
                        _token,
                        _owner,
                        _repo,
                        _release_branch,
                        _ver,
                        int(pr_number),
                    )
                )
            else:
                return False
            if not cleanup_confirmed:
                return False

            # Re-lock and prove this is still the same cleanup debt before
            # clearing it into a promotion_failed record.  A concurrent landing
            # or operator action wins without being overwritten.
            session.expire_all()
            locked: release_version.ReleaseVersion | None = (
                (
                    await session.execute(
                        select(release_version.ReleaseVersion)
                        .where(release_version.ReleaseVersion.id == version_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .first()
            )
            if locked is None or locked.status != "open":
                return False
            locked_qual = locked.last_qualification or {}
            locked_pending, locked_kind, _ = _canonicalize_promotion_pending(
                locked_qual.get("promotion_pending"),
                qualification=locked_qual,
                version=locked,
            )
            if (
                locked_kind != _PROMOTION_PENDING_MANAGED
                or locked_pending is None
                or locked_pending.get("pr_number") != int(pr_number)
                or not locked_pending.get("cleanup_required")
            ):
                return False
            locked_base_record = {
                k: v
                for k, v in locked_qual.items()
                if k != "promotion_pending"
            }
            locked_version_number = locked.version_number
            await _record_promotion_failure(
                session,
                locked,
                locked_base_record,
                str(locked_pending.get("timeout_error") or _timeout_error),
                detail=str(locked_pending.get("timeout_detail") or _timeout_detail),
                pr_url=locked_pending.get("pr_url"),
                lock_released=False,
            )
            await session.commit()
            logger.info(
                "[RELEASE_QUALIFIER] reconcile promotion_pending_timeout: "
                "confirmed PR #%s terminal, cleared cleanup debt, and returned "
                "v%d to stabilize",
                pr_number,
                locked_version_number,
            )
            return True
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile promotion_pending_timeout: "
                "unexpected error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile promotion_pending_timeout: "
                    "rollback failed for version %s after unexpected error",
                    version_id,
                )
            return False

# ---------------------------------------------------------------------------
# Post-promotion serving confirmation (moved to app.release_serving_confirmation)
# ---------------------------------------------------------------------------
