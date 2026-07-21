"""Reconciliation helpers for release qualification sweeps.

This module owns the out-of-band reconciliation paths that catch a release
version up when its promotion PR lands (or is terminally closed) outside the
normal qualification completion flow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app import github, pause_clock, release_version
from app.database import AsyncSessionLocal
from app.models import (
    DeploymentState,
    DeploymentStatus,
    Project,
    QualificationVerdict,
    ReleaseDeployArtifact,
    Task,
    TaskStatus,
)
from app.deploy_identity import deploy_repo_identity
from app.release_artifacts import (
    BACKEND_REVISION_ARTIFACT_KIND,
    aggregate_required_artifact_status,
    apply_serving_decision,
    artifact_is_unconfirmed,
    get_or_create_release_deploy_artifact,
    is_backend_revision_artifact,
    iter_release_artifact_providers,
    observable_release_artifact_providers,
    project_backend_revision_artifact,
    summarize_release_artifacts,
)
from app.release_promotion import (
    _DEPLOY_CONFIRM_WINDOW_SECONDS,
    _candidate_already_on_main,
    _delete_release_branch_best_effort,
    _idempotent_pr_url,
    _lts_branch_names,
    _promotion_pr_landed,
    _record_promotion_failure,
    _record_promotion_success,
    _recover_confidence_from_ci_unresolved,
    _resolve_github_with_session,
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

logger = logging.getLogger(__name__)

# Audit C9: hard wall-clock deadline on the ``promotion_pending`` sub-state.
#
# When a qualification PASSES and promotion fires but the promotion PR's CI is
# still pending (GitHub 405, no conflict), ``promote()`` enables auto-merge and
# leaves ``last_qualification.status == "pending"`` with a ``promotion_pending``
# sub-state so the reaper can confirm the merge later.  By design that sub-state
# *bypasses* the 6h qualification-timeout backstop
# (``_finalize_in_dedicated_session`` returns early when ``promotion_pending`` is
# present) — the qualifier children already terminated, so that backstop no
# longer applies and killing the version would discard a passing PR mid-CI.
#
# But nothing replaced that backstop for the ``promotion_pending`` state itself.
# The only recovery is ``_reconcile_if_pending_auto_merge_landed`` detecting a
# terminal PR state (merged / closed / CI-red); that path is a no-op when the PR
# stays open with pending/absent CI or when the GitHub calls fail, so a version
# can strand in ``promotion_pending`` indefinitely and block the whole
# self-improve loop while becoming operator-invisible after the single-fire
# stall alert.
#
# This deadline is the replacement backstop: once a ``promotion_pending``
# sub-state has aged past the cap we record a ``promotion_pending_timeout``
# failure, which clears ``promotion_pending`` (sending the version back to
# stabilize) and re-arms the normal qualification-timeout backstop + retry
# machinery on the next cycle.  The default (24h) is deliberately well beyond
# the 4h stall alert and any realistic CI duration so it only fires on a
# genuinely wedged promotion, never on a slow-but-progressing one.
_PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT = 24 * 3600
# A bare promotion CLAIM (``promotion_pending`` carrying ``claiming`` with no
# ``pr_number`` yet) is written under-lock by promote() immediately before it
# releases the row lock for its GitHub I/O, so a concurrent re-qualify cannot
# clobber the in-flight promotion.  IMPORTANT: on the straight-through success
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
_MANAGED_CHECK_WAIT_SNAPSHOT_DETAIL_LIMIT = 10
_MANAGED_CHECK_WAIT_DETAIL_TEXT_LIMIT = 160
# Number of consecutive sweeps where CI is red due to only transient conclusions
# (cancelled/timed_out) before the reconciler escalates to the normal terminal
# path.  Overridable via runtime_config["max_transient_ci_red_sweeps"].
_TRANSIENT_CI_RED_SWEEP_DEFAULT = 5
# Number of consecutive sweeps where the GitHub check-runs API returns an
# error before the reconciler surfaces a distinct warning so operators know
# that a green-but-stuck appearance may be an API-error masquerade.
_API_ERROR_SWEEP_WARN_THRESHOLD = 3


_TERMINAL_405_SUBSTRINGS = (
    "pull request is closed",
    "auto merge was disabled",
    "auto-merge was disabled",
    "not enabled",
    "merge commits are not allowed",
)
_RETRIABLE_405_SUBSTRINGS = (
    "required status check",
    "required checks",
    "base branch was recently updated",
    "waiting for status",
)


def _classify_405_message(msg: str) -> str:
    """Classify a GitHub 405 merge-refusal message as 'terminal' or 'retriable'.

    GitHub returns 405 for both permanent blockers (PR closed, auto-merge
    disabled, branch-protection config changed) and transient ones (required
    checks not yet passed).  A terminal 405 must be recorded as a distinct
    failure so operators see the real cause; a retriable 405 should leave the
    version pending for the next sweep.

    Returns 'terminal', 'retriable', or 'unknown' (treated as retriable by the
    caller so we never prematurely kill a version on an unrecognised message).
    """
    lower = msg.lower()
    for substr in _TERMINAL_405_SUBSTRINGS:
        if substr in lower:
            return "terminal"
    for substr in _RETRIABLE_405_SUBSTRINGS:
        if substr in lower:
            return "retriable"
    return "unknown"


def _trim_check_snapshot_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:_MANAGED_CHECK_WAIT_DETAIL_TEXT_LIMIT]


def _managed_check_wait_snapshot(
    *,
    ci_state: str,
    classified_at: datetime,
    head_sha: str | None = None,
    check_result: dict | None = None,
    reason: str | None = None,
    detail: str | None = None,
    from_api_error: bool = False,
    api_error_sweep_count: int = 0,
) -> dict:
    """Build a bounded operator snapshot for a non-terminal check-wait sweep."""

    details = []
    raw_details = (
        check_result.get("details") if isinstance(check_result, dict) else None
    )
    if isinstance(raw_details, list):
        for raw in raw_details[:_MANAGED_CHECK_WAIT_SNAPSHOT_DETAIL_LIMIT]:
            if not isinstance(raw, dict):
                continue
            item = {
                "name": _trim_check_snapshot_text(raw.get("name")) or "unknown",
            }
            status = _trim_check_snapshot_text(raw.get("status"))
            conclusion = _trim_check_snapshot_text(raw.get("conclusion"))
            if status is not None:
                item["status"] = status
            if conclusion is not None:
                item["conclusion"] = conclusion
            details.append(item)
    else:
        raw_details = []

    snapshot = {
        "state": ci_state,
        "classified_at": classified_at.isoformat(),
        "head_sha": head_sha,
        "total_checks": len(raw_details),
        "checks": details,
        "truncated": len(raw_details) > _MANAGED_CHECK_WAIT_SNAPSHOT_DETAIL_LIMIT,
    }
    if reason:
        snapshot["reason"] = reason
    if detail:
        snapshot["detail"] = detail[:_MANAGED_CHECK_WAIT_DETAIL_TEXT_LIMIT]
    if from_api_error:
        snapshot["api_error"] = True
        snapshot["api_error_sweep_count"] = api_error_sweep_count
    return snapshot


async def _persist_managed_check_wait_snapshot(
    session,
    version_id: uuid.UUID,
    *,
    pr_number: int,
    ci_state: str,
    snapshot: dict,
) -> bool:
    """Persist additive CI telemetry without clobbering promotion PR provenance."""

    try:
        # The row lock was released by the earlier commit, so this session may
        # still be holding a stale identity-map copy. Expire before the
        # FOR-UPDATE re-read so we evaluate the current DB state, not the cached
        # pre-I/O instance.
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
        qual = dict(locked.last_qualification or {})
        if qual.get("status") != "pending":
            return False
        pending = qual.get("promotion_pending")
        if not isinstance(pending, dict) or not pending.get("managed_check_wait"):
            return False
        if pending.get("pr_number") != pr_number:
            return False

        updated_pending = dict(pending)
        updated_pending["ci_state"] = ci_state
        updated_pending["ci_classified_at"] = snapshot.get("classified_at")
        updated_pending["check_snapshot"] = snapshot
        # Track consecutive API-error sweeps so the reconciler can distinguish
        # an api-error masquerade from genuine in-progress CI.
        if snapshot.get("api_error"):
            updated_pending["api_error_sweep_count"] = snapshot.get(
                "api_error_sweep_count", 1
            )
        else:
            updated_pending.pop("api_error_sweep_count", None)
        # Reset consecutive-transient-red counter whenever CI leaves the red
        # state so intermittent flakiness does not accumulate non-consecutively.
        if ci_state != _CI_RED:
            updated_pending.pop("transient_red_count", None)
        qual["promotion_pending"] = updated_pending
        locked.last_qualification = qual
        flag_modified(locked, "last_qualification")
        session.add(locked)
        await session.commit()
        return True
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: failed to persist "
            "check snapshot for version %s",
            version_id,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile managed_check_wait: rollback failed "
                "after check snapshot persist error for version %s",
                version_id,
            )
        return False


async def _reset_pending_transient_red_count(
    session,
    version_id: uuid.UUID,
    *,
    pr_number: int,
) -> None:
    """Reset ``promotion_pending.transient_red_count`` to 0 when CI is no longer red.

    Called whenever CI transitions away from red (green or pending) so that
    intermittent transient failures do not accumulate non-consecutively.
    """
    try:
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
            return
        qual = dict(locked.last_qualification or {})
        if qual.get("status") != "pending":
            return
        pending = qual.get("promotion_pending")
        if not isinstance(pending, dict):
            return
        if pending.get("pr_number") != pr_number:
            return
        if not pending.get("transient_red_count"):
            return
        updated_pending = dict(pending)
        updated_pending.pop("transient_red_count", None)
        qual["promotion_pending"] = updated_pending
        locked.last_qualification = qual
        flag_modified(locked, "last_qualification")
        session.add(locked)
        await session.commit()
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] transient_red_count: reset failed for version %s",
            version_id,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] transient_red_count: rollback after reset "
                "failed for version %s",
                version_id,
            )


async def _increment_pending_transient_red_count(
    session,
    version_id: uuid.UUID,
    *,
    pr_number: int,
) -> int:
    """Increment ``promotion_pending.transient_red_count`` for *version_id*.

    Returns the NEW count after increment, or 0 when the update cannot be
    applied (version no longer open/pending, PR number mismatch, etc.).
    """
    try:
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
            return 0
        qual = dict(locked.last_qualification or {})
        if qual.get("status") != "pending":
            return 0
        pending = qual.get("promotion_pending")
        if not isinstance(pending, dict):
            return 0
        if pending.get("pr_number") != pr_number:
            return 0
        updated_pending = dict(pending)
        new_count = (updated_pending.get("transient_red_count") or 0) + 1
        updated_pending["transient_red_count"] = new_count
        qual["promotion_pending"] = updated_pending
        locked.last_qualification = qual
        flag_modified(locked, "last_qualification")
        session.add(locked)
        await session.commit()
        return new_count
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] transient_red_count: failed to persist for version %s",
            version_id,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] transient_red_count: rollback failed for "
                "version %s",
                version_id,
            )
        return 0


async def _refresh_release_artifact_rows(
    session,
    version: release_version.ReleaseVersion,
    now: datetime,
    *,
    recheck_failed_required: bool = False,
) -> tuple[
    list[tuple[object, object]],
    bool,
    bool,
    object | None,
]:
    """Refresh artifact rows from provider checks without mutating version scalars.

    ``_confirm_serving_or_fail`` projects the backend artifact onto the legacy
    ``ReleaseVersion`` scalars while the multi-artifact registry is still
    unresolved, but recovery/admin clear paths cannot do that: they start from a
    latched aggregate ``deploy_failed`` scalar and must not transiently rewrite
    it back to backend-only truth before every required artifact re-confirms.
    This helper centralizes the provider sweep and returns the backend artifact
    for the caller to project or consume explicitly.
    """

    provider_rows = []
    had_required_unconfirmed = False
    mutated = False
    backend_artifact = None

    # Iterate only the providers OBSERVABLE for this version's project (the
    # deploy-level identity), not every registered provider.  This is the same
    # set armed at promotion: a non-observable provider (e.g. a Cloud Run signal
    # for an external project) must never get a row created or checked here, or
    # its unconfirmed row would strand the aggregate on a signal nothing can
    # read.  Resolving the deploy identity once per call keeps the sweep cheap.
    project = await session.get(Project, version.project_id)
    deploy_identity = deploy_repo_identity()

    observable_kinds: set[str] = set()
    for provider in observable_release_artifact_providers(project, deploy_identity):
        artifact, created = await get_or_create_release_deploy_artifact(
            session,
            version,
            provider.artifact_kind,
            confirmation_deadline=version.deploy_confirm_deadline,
        )
        if created:
            mutated = True
        if provider.required and artifact_is_unconfirmed(artifact):
            had_required_unconfirmed = True

        should_check = artifact_is_unconfirmed(artifact) or (
            recheck_failed_required
            and provider.required
            and artifact.serving_status in ("deploy_failed", "confirmation_timed_out")
        )
        if should_check:
            outcome = await provider.check(session, version, artifact, now)
            if outcome is not None:
                if apply_serving_decision(artifact, outcome, now=now):
                    mutated = True
                session.add(artifact)
        provider_rows.append((provider, artifact))
        observable_kinds.add(provider.artifact_kind)
        if is_backend_revision_artifact(provider.artifact_kind):
            backend_artifact = artifact

    # Also resolve any existing REQUIRED artifact rows whose provider is NOT
    # currently observable for this project.  Such rows were armed when the
    # deploy identity matched (or by a prior code version) but can never
    # self-confirm now.  Time them out at their confirmation_deadline with
    # reason_code="provider_unobservable" so the sweep aggregate and
    # summarize_release_artifacts converge and no armed row can outlive
    # resolution.  Within the deadline, count them as unconfirmed so the sweep
    # re-evaluates on each tick (the identity may be restored before deadline).
    all_required_provider_map = {
        p.artifact_kind: p for p in iter_release_artifact_providers() if p.required
    }
    unobservable_required_kinds = set(all_required_provider_map) - observable_kinds
    if unobservable_required_kinds:
        existing_unobservable_rows = (
            (
                await session.execute(
                    select(ReleaseDeployArtifact).where(
                        ReleaseDeployArtifact.version_id == version.id,
                        ReleaseDeployArtifact.artifact_kind.in_(
                            unobservable_required_kinds
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        _now_naive = _to_naive_utc(now) or now
        for artifact in existing_unobservable_rows:
            provider = all_required_provider_map[artifact.artifact_kind]
            if not artifact_is_unconfirmed(artifact):
                # Already resolved — include in provider_rows for aggregate.
                provider_rows.append((provider, artifact))
                continue
            # Unconfirmed unobservable required row: record it as having been
            # unconfirmed so _confirm_serving_or_fail proceeds to compute the
            # aggregate (rather than bailing at the had_required_unconfirmed
            # guard), then resolve or leave it depending on the deadline.
            had_required_unconfirmed = True
            artifact_deadline = _to_naive_utc(
                artifact.confirmation_deadline or version.deploy_confirm_deadline
            )
            if artifact_deadline is not None and _now_naive >= artifact_deadline:
                # Past deadline: resolve with provider_unobservable so the row
                # is no longer pending and the aggregate can converge.
                decision: tuple = (
                    "confirmation_timed_out",
                    "provider_unobservable",
                    None,
                )
                if apply_serving_decision(artifact, decision, now=now):
                    mutated = True
                session.add(artifact)
            # Within deadline: count as unconfirmed (identity may be restored
            # before the window expires); had_required_unconfirmed already set.
            provider_rows.append((provider, artifact))

    return provider_rows, had_required_unconfirmed, mutated, backend_artifact


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
    from individual check conclusions, so a transient-only outcome is correctly
    excluded.  When details are absent or empty the aggregate ``conclusion``
    field is used as a fallback — preserving existing behaviour for callers that
    supply no per-check detail list.
    """
    if not isinstance(check_result, dict):
        return False
    genuine = {"failure", "action_required"}
    details = check_result.get("details")
    if isinstance(details, list) and details:
        return any(
            isinstance(d, dict) and d.get("conclusion") in genuine for d in details
        )
    # No per-check details — fall back to the aggregate conclusion.
    return check_result.get("conclusion") in genuine


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

            if not await _candidate_already_on_main(
                token, owner, repo, candidate_sha, version, project=project
            ):
                return False

            pr_number = _pr_number_from_url(base_record.get("pr_url"))
            if pr_number is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile: candidate %s for v%d is "
                    "on main, but the promotion_failed PR number could not be "
                    "parsed from %r — deferring live target validation",
                    candidate_sha[:8],
                    version.version_number,
                    base_record.get("pr_url"),
                )
                return False

            alembic_status, alembic_reason = await _validate_migration_chain_post_merge(
                token,
                owner,
                repo,
                pr_number,
                promotion_target,
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

            _integration_branch, promotion_target = _lts_branch_names(project)
            promoted_sha: str | None = None
            try:
                promoted_sha = await github.get_branch_sha(
                    token,
                    owner,
                    repo,
                    promotion_target,
                    project_id=str(version.project_id),
                    live=True,
                )
            except Exception as exc:  # noqa: BLE001 — fallback to candidate_sha
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile: could not resolve %s HEAD "
                    "SHA for v%d (%s) — using candidate_sha as promoted_sha",
                    promotion_target,
                    version.version_number,
                    exc,
                )

            result = await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                pr_url=_idempotent_pr_url(base_record),
                token=token,
                owner=owner,
                repo=repo,
                promoted_sha=promoted_sha,
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
    marker write after a release PR was created and auto-merge was armed on
    GitHub. A subsequent finalize saw no promotion_pending, re-evaluated CI as
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

            if not await _candidate_already_on_main(
                token, owner, repo, candidate_sha, version, project=project
            ):
                return False

            logger.info(
                "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: "
                "candidate %s for v%d is on main despite not_promoted/ci_unresolved "
                "record — reconciling as promoted",
                candidate_sha[:8],
                version.version_number,
            )
            promoted_sha: str | None = None
            try:
                promoted_sha = await github.get_branch_sha(
                    token,
                    owner,
                    repo,
                    promotion_target,
                    project_id=str(version.project_id),
                    live=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile not_promoted/ci_unresolved: "
                    "could not resolve %s HEAD SHA for v%d (%s) — using candidate_sha",
                    promotion_target,
                    version.version_number,
                    exc,
                )

            result = await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                pr_url=_idempotent_pr_url(base_record),
                token=token,
                owner=owner,
                repo=repo,
                promoted_sha=promoted_sha,
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


async def _reconcile_if_ci_unresolved_now_green(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Re-qualify a quiescent not_promoted/ci_unresolved version once CI is green.

    ``ci_unresolved`` means the prior finalization saw pending or absent CI, not
    a settled quality failure.  If automain still points at that exact candidate
    SHA and CI later resolves green, re-run qualification on the same candidate.
    When automain has advanced, the existing stale-not-promoted re-drive owns the
    fresh-SHA path.

    Returns True iff a new qualification was spawned.
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
            if version.status != "open" or version.phase != "stabilize":
                return False
            qual = version.last_qualification or {}
            if qual.get("status") != "not_promoted":
                return False
            if qual.get("blocker") != "ci_unresolved":
                return False
            candidate_sha: str | None = qual.get("candidate_sha")
            if not candidate_sha:
                return False

            project = await session.get(Project, version.project_id)
            if project is None:
                return False
            integration_branch, _promotion_target = _lts_branch_names(project)

            try:
                await session.commit()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile ci_unresolved_now_green: "
                    "failed to release row lock for v%d — skipping",
                    version.version_number,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] reconcile ci_unresolved_now_green: "
                        "rollback failed for v%d after commit error",
                        version.version_number,
                    )
                return False

            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile ci_unresolved_now_green: "
                    "no GitHub credentials for v%d — skipping",
                    version.version_number,
                )
                return False
            token, owner, repo = resolved

            try:
                automain_sha = await github.get_branch_sha(
                    token,
                    owner,
                    repo,
                    integration_branch,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile ci_unresolved_now_green: "
                    "could not resolve %s HEAD SHA for v%d (%s) — skipping",
                    integration_branch,
                    version.version_number,
                    exc,
                )
                return False
            if automain_sha != candidate_sha:
                return False

            from app import release_qualifier as _rq_mod

            ci_resolution = await _rq_mod._resolve_candidate_ci(
                token,
                owner,
                repo,
                candidate_sha,
                project_id=str(version.project_id),
            )
            if ci_resolution.get("state") == _CI_NO_CHECKS:
                # Integration-branch candidates structurally carry no check-runs
                # when the CI workflow's automain trigger has been removed.
                # Annotate with topology and proceed if CI is not required
                # (topology-relaxed no_checks), mirroring _ci_required_for_finalize.
                ci_resolution = (
                    await _rq_mod._annotate_absent_ci_integration_topology(
                        token=token,
                        owner=owner,
                        repo=repo,
                        project=project,
                        candidate_sha=candidate_sha,
                        ci_resolution=ci_resolution,
                    )
                )
                if _rq_mod._ci_required_for_finalize(project, ci_resolution):
                    return False
            elif ci_resolution.get("state") != _CI_GREEN or not ci_resolution.get(
                "ci_green"
            ):
                return False

            logger.info(
                "[RELEASE_QUALIFIER] reconcile ci_unresolved_now_green: "
                "candidate %s for v%d now has green CI or topology-relaxed "
                "no_checks on unchanged automain — spawning fresh qualification",
                candidate_sha[:8],
                version.version_number,
            )
            result = await _rq_mod.run_qualification(
                session, version.project_id, version
            )
            if result and result.get("status") == "spawned":
                await session.commit()
                return True
            await session.rollback()
            return False
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile ci_unresolved_now_green: "
                "unexpected error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile ci_unresolved_now_green: "
                    "rollback failed for version %s after unexpected error",
                    version_id,
                )
            return False


async def _reconcile_if_pending_auto_merge_landed(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Reconcile an open version whose promotion PR has actually merged."""
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
            pending = qual.get("promotion_pending")
            if not pending:
                return False
            # managed_check_wait records are owned by _reconcile_if_managed_check_wait
            # (they require a re-run of the Alembic guard before merging and must
            # NOT be merged via native auto-merge).  Skip them here.
            if pending.get("managed_check_wait"):
                return False
            pr_number = pending.get("pr_number")
            if not pr_number:
                return False
            candidate_sha: str | None = qual.get("candidate_sha")
            if not candidate_sha:
                return False

            base_record = {k: v for k, v in qual.items() if k != "promotion_pending"}
            pr_url: str = pending.get("pr_url") or _idempotent_pr_url(base_record)

            project = await session.get(Project, version.project_id)
            if project is None:
                return False

            # Single-branch repo guard (LTS model inactive) — mirrors the
            # promote() guard.  When the integration branch IS the promotion
            # target promote() never opens a release PR (it records an inert
            # success), so there is no pending promotion PR to reconcile and
            # _candidate_already_on_main would always be True on the single
            # branch.  Bail out inert, mirroring the back-merge guard's
            # "lts_inactive" skip semantics.
            integration_branch, promotion_target = _lts_branch_names(project)
            if integration_branch == promotion_target:
                logger.debug(
                    "[RELEASE_QUALIFIER] reconcile pending_auto_merge: v%d "
                    "integration branch (%s) is the promotion target — "
                    "single-branch repo, LTS inactive; nothing to reconcile",
                    version.version_number,
                    promotion_target,
                )
                return False

            try:
                await session.commit()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile pending_auto_merge: failed to "
                    "release row lock for v%d before GitHub I/O — skipping",
                    version.version_number,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] reconcile pending_auto_merge: rollback "
                        "failed for v%d after commit error",
                        version.version_number,
                    )
                return False

            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile pending_auto_merge: no GitHub "
                    "credentials for v%d — skipping reconcile",
                    version.version_number,
                )
                return False
            token, owner, repo = resolved

            if await _candidate_already_on_main(
                token, owner, repo, candidate_sha, version, project=project
            ):
                (
                    alembic_status,
                    alembic_reason,
                ) = await _validate_migration_chain_post_merge(
                    token,
                    owner,
                    repo,
                    int(pr_number),
                    promotion_target,
                )
                if alembic_status == "defer":
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile pending_auto_merge: "
                        "candidate %s for v%d is on main, but post-merge "
                        "Alembic health check is deferred: %s",
                        candidate_sha[:8],
                        version.version_number,
                        alembic_reason,
                    )
                    return False
                if alembic_status == "block":
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile pending_auto_merge: "
                        "candidate %s for v%d is on main, but the live target "
                        "Alembic chain has a genuine third-party collision — "
                        "recording promotion_failed: %s",
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
                        pr_url=pr_url,
                        lock_released=True,
                    )
                    await session.commit()
                    return True

                logger.info(
                    "[RELEASE_QUALIFIER] reconcile pending_auto_merge: candidate "
                    "%s for v%d is on main — recording promotion",
                    candidate_sha[:8],
                    version.version_number,
                )
                _integration_branch, promotion_target = _lts_branch_names(project)
                promoted_sha: str | None = None
                try:
                    promoted_sha = await github.get_branch_sha(
                        token,
                        owner,
                        repo,
                        promotion_target,
                        project_id=str(version.project_id),
                        live=True,
                    )
                except Exception as exc:  # noqa: BLE001 — fallback to candidate_sha
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile pending_auto_merge: could "
                        "not resolve %s HEAD SHA for v%d (%s) — using "
                        "candidate_sha as promoted_sha",
                        promotion_target,
                        version.version_number,
                        exc,
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
                return result.get("status") == "promoted" and not result.get(
                    "already_promoted"
                )

            try:
                pr_data = await github.get_pull_request(
                    token,
                    owner,
                    repo,
                    int(pr_number),
                    project_id=str(version.project_id),
                )
                if pr_data.get("state") == "closed" and not pr_data.get("merged"):
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile pending_auto_merge: PR #%s "
                        "for v%d is closed without merge — driving to "
                        "promotion_failed so retry machinery applies",
                        pr_number,
                        version.version_number,
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
                    return True

                if pr_data.get("state") == "open":
                    pr_head_sha: str | None = (pr_data.get("head") or {}).get("sha")
                    if pr_head_sha:
                        try:
                            check_result = await github.get_check_runs_cached(
                                token,
                                owner,
                                repo,
                                pr_head_sha,
                                project_id=str(version.project_id),
                                treat_missing_as_success=False,
                            )
                            ci_state = _classify_check_runs(check_result)
                            if ci_state != _CI_RED:
                                await _reset_pending_transient_red_count(
                                    session,
                                    version.id,
                                    pr_number=int(pr_number),
                                )
                            if ci_state == _CI_RED:
                                if not _check_result_has_genuine_failure(check_result):
                                    # Only transient conclusions (cancelled /
                                    # timed_out) — do not tear down the PR.
                                    new_count = (
                                        await _increment_pending_transient_red_count(
                                            session,
                                            version.id,
                                            pr_number=int(pr_number),
                                        )
                                    )
                                    max_sweeps = _max_transient_ci_red_sweeps()
                                    if new_count < max_sweeps:
                                        logger.warning(
                                            "[RELEASE_QUALIFIER] reconcile "
                                            "pending_auto_merge: PR #%s for v%d has "
                                            "transient-only red CI (cancelled/"
                                            "timed_out, sweep %d/%d) — leaving "
                                            "pending",
                                            pr_number,
                                            version.version_number,
                                            new_count,
                                            max_sweeps,
                                        )
                                        return False
                                    logger.warning(
                                        "[RELEASE_QUALIFIER] reconcile "
                                        "pending_auto_merge: PR #%s for v%d reached "
                                        "transient-red sweep cap (%d) — escalating "
                                        "to pr_ci_red",
                                        pr_number,
                                        version.version_number,
                                        max_sweeps,
                                    )
                                logger.warning(
                                    "[RELEASE_QUALIFIER] reconcile pending_auto_merge: "
                                    "PR #%s for v%d has conclusively failed CI — "
                                    "driving to promotion_failed so retry machinery "
                                    "applies",
                                    pr_number,
                                    version.version_number,
                                )
                                await _record_promotion_failure(
                                    session,
                                    version,
                                    base_record,
                                    "pr_ci_red",
                                    detail=(
                                        f"PR #{pr_number} CI is conclusively failed "
                                        f"(sha={pr_head_sha[:8]}); auto-merge will "
                                        "never fire"
                                    ),
                                    pr_url=pr_url,
                                    lock_released=True,
                                )
                                await session.commit()
                                return True
                        except Exception as exc:  # noqa: BLE001 — CI fetch failure → leave pending
                            logger.warning(
                                "[RELEASE_QUALIFIER] reconcile pending_auto_merge: "
                                "could not fetch CI for PR #%s head %s for v%d "
                                "(%s) — leaving pending",
                                pr_number,
                                pr_head_sha[:8] if pr_head_sha else "unknown",
                                version.version_number,
                                exc,
                            )
            except Exception as exc:  # noqa: BLE001 — PR state check must not abort sweep
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile pending_auto_merge: could not "
                    "check PR #%s state for v%d (%s) — leaving pending",
                    pr_number,
                    version.version_number,
                    exc,
                )

            return False
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] reconcile pending_auto_merge: unexpected "
                "error for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] reconcile pending_auto_merge: rollback "
                    "failed for version %s after unexpected error",
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
) -> None:
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
            return

        # ── Infra-flake guard (Fix X) ──────────────────────────────────────────
        # Before spawning a code-fix task, check whether the promotion CI failure
        # is a transient infrastructure flake (npm registry unreachable,
        # Docker Hub rate-limit, Playwright install failure, setup-step only).
        # If so, skip the fix spawn and immediately re-qualify (which opens a fresh
        # promotion PR with fresh CI) — a code change cannot fix an infra flake,
        # and spawning a doomed agent wastes a retry slot.
        #
        # NOTE: by the time this harvest runs, _cleanup_managed_check_wait_failure_
        # artifacts has already closed the promotion PR and deleted the release
        # branch.  Re-running the dead workflow via rerun_failed_jobs is useless;
        # instead, schedule a new qualification run directly so a fresh release
        # branch and promotion PR are created.  The Z circuit breaker in
        # _redrive_promotion_failed_qualification caps total re-qualify attempts.
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
                if _setup_flake or _content_flake:
                    _flake_reason = (
                        "setup_phase_failure" if _setup_flake else "infra_log_patterns"
                    )
                    logger.info(
                        "[RELEASE_QUALIFIER] promotion-CI harvest: v%d CI failure "
                        "is an infrastructure flake (%s) — scheduling fresh "
                        "re-qualification (new promotion PR) and skipping fix "
                        "spawn (source %s)",
                        version_number,
                        _flake_reason,
                        ci_source_sha[:8],
                    )
                    # Trigger a fresh qualification immediately so the version
                    # gets a new promotion PR rather than waiting for the next
                    # sweep cycle.  "promotion_failed" is not a conclusive
                    # verdict status so run_qualification proceeds even if the
                    # candidate SHA has not changed.  Best-effort; the Z
                    # re-driver in sweep_stuck_qualifications is the fallback.
                    try:
                        from app import release_qualifier as _rq_mod
                        from app.database import AsyncSessionLocal as _ASL

                        async with _ASL() as _rq_session:
                            _rq_version = await _rq_session.get(
                                release_version.ReleaseVersion, version_id
                            )
                            if _rq_version is not None:
                                _rq_result = await _rq_mod.run_qualification(
                                    _rq_session, project_id, _rq_version
                                )
                                if _rq_result and _rq_result.get("status") == "spawned":
                                    await _rq_session.commit()
                                    logger.info(
                                        "[RELEASE_QUALIFIER] promotion-CI harvest: "
                                        "v%d infra-flake re-qualify spawned (%s)",
                                        version_number,
                                        _flake_reason,
                                    )
                                else:
                                    await _rq_session.rollback()
                                    logger.info(
                                        "[RELEASE_QUALIFIER] promotion-CI harvest: "
                                        "v%d infra-flake re-qualify deferred "
                                        "(run_qualification status=%s) — Z "
                                        "re-driver will retry next sweep",
                                        version_number,
                                        _rq_result.get("status") if _rq_result else None,
                                    )
                    except Exception:
                        logger.warning(
                            "[RELEASE_QUALIFIER] promotion-CI harvest: v%d "
                            "infra-flake re-qualify attempt failed — Z re-driver "
                            "will retry next sweep",
                            version_number,
                            exc_info=True,
                        )
                    return
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
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] promotion-CI harvest failed for v%d — the "
            "promotion failure is already recorded; continuing",
            version_number,
        )


async def _reconcile_if_managed_check_wait(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Reconcile a migration-carrying promotion PR in managed_check_wait mode.

    For a pending version with ``promotion_pending.managed_check_wait=True``
    (set by ``promote()`` instead of delegating to GitHub native auto-merge),
    this reconciler periodically re-checks whether the required CI has passed
    and re-runs the Alembic dual-head guard before retrying the merge.

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
            pending = qual.get("promotion_pending")
            if not pending or not pending.get("managed_check_wait"):
                return False
            pr_number = pending.get("pr_number")
            if not pr_number:
                return False
            candidate_sha: str | None = qual.get("candidate_sha")
            if not candidate_sha:
                return False

            base_record = {k: v for k, v in qual.items() if k != "promotion_pending"}
            pr_url: str = pending.get("pr_url") or _idempotent_pr_url(base_record)

            project = await session.get(Project, version.project_id)
            if project is None:
                return False

            integration_branch, promotion_target = _lts_branch_names(project)
            if integration_branch == promotion_target:
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
            _harvest_ar_config = dict(
                (project.design_doc or {}).get("_auto_remediation", {}) or {}
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

            # 1. Check out-of-band landing first — the PR may have been merged
            #    externally while we were waiting.
            if await _candidate_already_on_main(
                token, owner, repo, candidate_sha, version, project=project
            ):
                (
                    alembic_status,
                    alembic_reason,
                ) = await _validate_migration_chain_post_merge(
                    token, owner, repo, int(pr_number), promotion_target
                )
                if alembic_status == "defer":
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: "
                        "candidate %s for v%d on main, post-merge Alembic "
                        "deferred: %s",
                        candidate_sha[:8],
                        version.version_number,
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
                        int(pr_number),
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
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: candidate "
                    "%s for v%d is already on main (out-of-band) — recording "
                    "promoted",
                    candidate_sha[:8],
                    version.version_number,
                )
                promoted_sha_oob: str | None = None
                try:
                    promoted_sha_oob = await github.get_branch_sha(
                        token,
                        owner,
                        repo,
                        promotion_target,
                        project_id=str(version.project_id),
                        live=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: could "
                        "not resolve %s HEAD for v%d (%s)",
                        promotion_target,
                        version.version_number,
                        exc,
                    )
                result_oob = await _record_promotion_success(
                    session,
                    version,
                    candidate_sha,
                    base_record,
                    pr_url,
                    token=token,
                    owner=owner,
                    repo=repo,
                    promoted_sha=promoted_sha_oob,
                    already_landed=True,
                    lock_released=True,
                )
                await session.commit()
                return result_oob.get("status") == "promoted" and not result_oob.get(
                    "already_promoted"
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

            pr_head_sha: str | None = (pr_data.get("head") or {}).get("sha")
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

            # 5. Attempt the merge.
            try:
                merge_result = await github.merge_pull_request(
                    token,
                    owner,
                    repo,
                    int(pr_number),
                    merge_method="merge",
                    project_id=str(version.project_id),
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
                if await _promotion_pr_landed(
                    token,
                    owner,
                    repo,
                    int(pr_number),
                    candidate_sha,
                    version,
                    project=project,
                ):
                    logger.info(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                        "for v%d returned merged=False but is in fact merged — "
                        "recording success (idempotent)",
                        pr_number,
                        version.version_number,
                    )
                    # Fall through to the success block below.
                    merge_result = {"merged": True}
                elif merge_result.get("conflict"):
                    msg = merge_result.get("message", "merge not completed")
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s "
                        "for v%d has a real conflict — recording merge_blocked",
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
                        "merge_blocked",
                        detail=str(msg)[:300],
                        pr_url=pr_url,
                        conflict=True,
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
            promoted_sha_new: str | None = None
            try:
                promoted_sha_new = await github.get_branch_sha(
                    token,
                    owner,
                    repo,
                    promotion_target,
                    project_id=str(version.project_id),
                    live=True,
                )
            except Exception as exc:  # noqa: BLE001 — fallback to candidate_sha
                logger.warning(
                    "[RELEASE_QUALIFIER] reconcile managed_check_wait: could not "
                    "resolve %s HEAD for v%d (%s)",
                    promotion_target,
                    version.version_number,
                    exc,
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
) -> None:
    """Close the managed-check promotion PR and delete its branch best-effort."""
    try:
        await github.close_pull_request(
            token,
            owner,
            repo,
            pr_number,
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — cleanup must not block failure recording
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


async def _reconcile_if_pending_qualification_superseded(
    version_id: uuid.UUID,
    *,
    session_factory=None,
) -> bool:
    """Reconcile an open version whose pending qualification candidate landed out-of-band.

    When an operator force-promotes a candidate to the promotion target without
    going through the normal qualification -> promotion_pending -> auto-merge
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
            # PR — that path is owned by _reconcile_if_pending_auto_merge_landed
            # and _reconcile_if_promotion_pending_timed_out.
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
            # audit+smoketest-only check diverged from the canonical in-flight
            # definition (_qualification_in_flight, which also covers merge_grade
            # and challenge) — so an attempt whose audit/smoke had finished but
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
            smoketest_required = bool(qual.get("smoketest_enabled", True))
            in_flight_child_keys = ["audit_task_id"]
            if smoketest_required:
                in_flight_child_keys.append("smoketest_task_id")
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
    docstring on ``_PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT``).  Pure DB work —
    no GitHub I/O — so it fires even when GitHub calls fail (the exact case the
    ``_reconcile_if_pending_auto_merge_landed`` terminal-state path cannot
    handle).

    For an ``open`` version with ``last_qualification.status == "pending"`` and a
    ``promotion_pending`` sub-state whose ``enabled_at`` age exceeds the
    configurable cap, records a ``_record_promotion_failure`` with error
    ``promotion_pending_timeout``.  Because ``base_record`` excludes
    ``promotion_pending``, that failure CLEARS the sub-state — the version goes
    back to stabilize, and on the next cycle the normal qualification-timeout
    backstop + retry machinery are re-armed.  ``main`` is never touched, so this
    is always safe.  Returns True iff a timeout failure was recorded.
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
            pending = qual.get("promotion_pending")
            if not pending:
                return False

            enabled_at_str = pending.get("enabled_at")
            if not enabled_at_str:
                # No clock to age against — leave it; the auto-merge stall alert
                # (which has the same missing-enabled_at guard) surfaces this.
                return False
            try:
                enabled_at = datetime.fromisoformat(enabled_at_str)
            except (ValueError, TypeError):
                return False

            cap_seconds = _promotion_pending_timeout_seconds()
            # A bare promotion CLAIM (``claiming`` + no ``pr_number``) is only
            # ever alive for promote()'s 30-120s I/O window; if it has aged past
            # the short claim bound, promote() crashed between claiming and
            # opening the PR, so GC it now instead of letting it inherit the 24h
            # cap and wedge the version out of re-qualification.
            _is_bare_claim = bool(pending.get("claiming")) and not pending.get(
                "pr_number"
            )
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
                return False

            age_hours = age_seconds / 3600.0
            pr_number = pending.get("pr_number")
            pr_url: str | None = pending.get("pr_url")
            # A migration-carrying promotion held in managed_check_wait is owned
            # by _reconcile_if_managed_check_wait, but that reconciler has no hard
            # timeout of its own, so this pure-DB backstop is its only escape from
            # a promotion that never reaches a terminal CI/merge signal.  Unlike
            # the managed reconciler's red/closed paths, this backstop does NOT
            # close the PR — so after it clears promotion_pending the promotion PR
            # is left OPEN, and the promotion_failed re-driver would then open a
            # FRESH promotion PR that races the orphan (wasting a full audit +
            # merge-grade on the duplicate).  Remember whether this is a managed
            # record so we can best-effort tear the orphan down after the lock is
            # released.
            _managed_check_wait = bool(pending.get("managed_check_wait"))
            # base_record drops promotion_pending (mirrors
            # _reconcile_if_pending_auto_merge_landed) so the recorded failure
            # CLEARS the sub-state and re-arms the qualification-timeout backstop.
            base_record = {k: v for k, v in qual.items() if k != "promotion_pending"}

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

            # Best-effort: close the orphaned managed-check-wait promotion PR and
            # delete its release branch now that the row lock is released (no
            # GitHub I/O is held under the lock).  Fresh PK reloads avoid touching
            # the commit-expired ORM instances (MissingGreenlet).  Fail-open: any
            # error leaves today's behavior (PR closed by a later sweep / operator)
            # and never turns the recorded timeout into an exception.
            if _managed_check_wait and pr_number:
                try:
                    _ver = await session.get(
                        release_version.ReleaseVersion, version_id
                    )
                    _proj = (
                        await session.get(Project, _ver.project_id)
                        if _ver is not None
                        else None
                    )
                    if _ver is not None and _proj is not None:
                        _resolved = await _resolve_github_with_session(session, _proj)
                        if _resolved is not None:
                            _token, _owner, _repo = _resolved
                            await _cleanup_managed_check_wait_failure_artifacts(
                                _token,
                                _owner,
                                _repo,
                                f"release-v{_ver.version_number}",
                                _ver,
                                int(pr_number),
                            )
                            logger.info(
                                "[RELEASE_QUALIFIER] reconcile "
                                "promotion_pending_timeout: closed orphaned "
                                "managed_check_wait promotion PR #%s and deleted "
                                "release-v%d so the re-qualify does not race it",
                                pr_number,
                                _ver.version_number,
                            )
                except Exception:
                    logger.warning(
                        "[RELEASE_QUALIFIER] reconcile promotion_pending_timeout: "
                        "best-effort cleanup of orphaned managed_check_wait PR #%s "
                        "failed — leaving it for a later sweep / operator",
                        pr_number,
                        exc_info=True,
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
# Post-promotion serving confirmation
# ---------------------------------------------------------------------------


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    """Strip tzinfo so comparisons against the naive-UTC DeploymentState match."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _classify_serving_outcome(
    ds: DeploymentState | None,
    promoted_at: datetime | None,
    promoted_sha: str | None,
    deadline: datetime | None,
    now: datetime,
) -> tuple[str, str | None, datetime | None] | None:
    """Derive a post-promotion serving outcome from the deploy-health signal.

    Pure function (no I/O) so the decision is directly unit-testable.  Returns:

      * ``None`` — no decision yet: still inside the confirmation window and no
        new healthy revision has booted (keep waiting).
      * ``("serving_confirmed", served_sha, served_at)`` — a new revision booted
        AFTER ``promoted_at``, is STABLE, and its live serving SHA was
        INDEPENDENTLY observed to match ``promoted_sha``.  ``served_sha`` is
        always that observed value, never a ``promoted_sha`` stand-in.
      * ``("deploy_failed", reason)`` (third element ``None``) — the deploy
        ACTIVELY went bad: a ROLLED_BACK/HALTED state arose after the promotion
        (``reason`` ``rolled_back``/``halted``), or the observed serving SHA does
        not match the promoted SHA (``sha_mismatch``).  This is a real failure
        the operator/strategist must treat as a crisis (roll forward / recover).
      * ``("confirmation_timed_out", reason)`` (third element ``None``) — the
        bounded confirmation window elapsed WITHOUT a positive confirmation but
        also WITHOUT any active failure signal: no new healthy revision appeared
        by ``deadline`` (``no_new_revision``), or a new revision went STABLE but
        its serving SHA could never be independently observed
        (``serving_sha_unobservable`` — e.g. the deploy SHA could not be read).
        This is NOT a failed deploy — the code likely shipped but is unverified;
        it must be surfaced and treated DIFFERENTLY from ``deploy_failed`` (a
        softer "verify serving" signal, not a roll-forward crisis), and it is
        recoverable to ``serving_confirmed`` if the SHA is later observed serving.

    Backstop: a promoted version with no explicit ``deadline`` would otherwise be
    undecidable forever (the timeout branch can never fire).  When ``deadline``
    is missing, fall back to ``promoted_at + _DEPLOY_CONFIRM_WINDOW_SECONDS`` so
    confirmation is always bounded — no version can wedge in ``unconfirmed``.
    """
    promoted_at = _to_naive_utc(promoted_at)
    deadline = _to_naive_utc(deadline)
    now = _to_naive_utc(now) or now

    # Backstop the confirmation window: a promotion recorded without a stamped
    # deadline (legacy / a promotion path that skipped arming it) must still time
    # out, or it sits unconfirmed forever and gates the next version.
    if deadline is None and promoted_at is not None:
        deadline = promoted_at + timedelta(seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS)

    # A new revision went STABLE after the promotion, but its live serving SHA
    # could not be independently observed (Cloud Run lookup degraded to no
    # value).  Tracked so the deadline path lands a distinct, honest failure
    # reason instead of confirming with a fabricated served SHA.
    serving_sha_unobservable = False

    if ds is not None:
        deployed_at = _to_naive_utc(ds.deployed_at)
        halted_at = _to_naive_utc(getattr(ds, "halted_at", None))
        status = ds.status
        revision_after_promotion = (
            deployed_at is not None
            and promoted_at is not None
            and deployed_at > promoted_at
        )

        # A HALT or ROLLBACK that occurred at/after the promotion is a hard
        # failure regardless of the deadline — the deploy actively went bad.
        if status == DeploymentStatus.HALTED:
            ref = halted_at or deployed_at
            if ref is not None and promoted_at is not None and ref >= promoted_at:
                return ("deploy_failed", "halted", None)
        if (
            status == DeploymentStatus.ROLLED_BACK
            and deployed_at is not None
            and promoted_at is not None
            and deployed_at >= promoted_at
        ):
            return ("deploy_failed", "rolled_back", None)

        # SERVING CONFIRMED: the deployment is STABLE and the live serving SHA
        # independently matches the promoted SHA.  A pre-promotion revision
        # CANNOT serve the promoted merge SHA (that SHA did not exist before
        # the promotion commit), so an exact SHA match is positive evidence
        # regardless of deployed_at/promoted_at ordering.  The race case where
        # the Cloud Run revision booted before the orchestrator finished
        # recording promoted_at (deployed_at <= promoted_at) is safe to
        # confirm: the matched SHA proves this is the right revision.
        #
        # SHA-mismatch and serving_sha_unobservable still require
        # revision_after_promotion to guard against a stale pre-promotion
        # revision that happens to be running a different SHA — in that case
        # we just wait, we do not fail the deploy.
        if status == DeploymentStatus.STABLE:
            current_sha = ds.current_sha or None
            if current_sha and promoted_sha:
                if current_sha == promoted_sha:
                    return ("serving_confirmed", current_sha, deployed_at)
                # SHA mismatch is a hard failure only for a revision that
                # booted AFTER the promotion was stamped — a pre-promotion
                # revision still running a different SHA is just "waiting".
                if revision_after_promotion:
                    return ("deploy_failed", "sha_mismatch", None)
            elif revision_after_promotion:
                # New revision booted after promotion but serving SHA is
                # unobservable (Cloud Run lookup degraded) — not confirmable.
                serving_sha_unobservable = True

    # No confirmation yet — once the bounded window elapses this is a TIMEOUT,
    # not an active failure: nothing observed the deploy going bad, the promoted
    # SHA just could not be confirmed serving in time.  Surface it distinctly
    # from deploy_failed (rolled_back/halted/sha_mismatch above) so it reads as
    # "verify serving", not "recover a failed deploy".
    if deadline is not None and now >= deadline:
        if serving_sha_unobservable:
            return ("confirmation_timed_out", "serving_sha_unobservable", None)
        return ("confirmation_timed_out", "no_new_revision", None)
    return None


async def _record_post_promotion_outcome(
    session,
    version: release_version.ReleaseVersion,
    outcome: str,
    now: datetime,
) -> None:
    """Best-effort: stamp ``post_promotion_outcome`` on the version's verdict row.

    Records the resolved serving outcome on the immutable
    ``QualificationVerdict`` history (the row written when the version promoted)
    without mutating any prior immutable field.  Mirrors
    ``_record_qualification_verdict``'s SAVEPOINT discipline: the caller's
    already-staged ReleaseVersion mutations are flushed into the outer
    transaction first, then the verdict update runs inside ``begin_nested`` so a
    failure (e.g. no verdict row) rolls back only the nested savepoint and never
    the serving-status write.
    """
    try:
        verdict = (
            (
                await session.execute(
                    select(QualificationVerdict)
                    .where(
                        QualificationVerdict.release_version_id == version.id,
                        QualificationVerdict.terminal_status == "promoted",
                    )
                    .order_by(QualificationVerdict.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if verdict is None:
            return
        await session.flush()
        async with session.begin_nested():
            verdict.post_promotion_outcome = outcome
            verdict.post_promotion_outcome_at = now
            session.add(verdict)
            await session.flush()
    except Exception as exc:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.warning(
            "[RELEASE_QUALIFIER] failed to record post-promotion outcome for v%s "
            "(non-fatal — additive history only): %s",
            getattr(version, "version_number", "?"),
            exc,
        )


async def _confirm_serving_or_fail(
    version_id: uuid.UUID,
    *,
    session_factory=None,
    now: datetime | None = None,
) -> bool:
    """Resolve a provisional (``serving_status="unconfirmed"``) promotion.

    For a ``promoted`` version still awaiting serving confirmation, derive the
    outcome from the DeploymentState singleton (the existing deploy-health
    serving signal) and either confirm it serving or land a distinct loud
    deploy-failed state.  Runs in its own dedicated session under a row lock —
    the same per-version isolation pattern as the reconcile loops above — so a
    failure on one version cannot touch another's committed work.

    Returns True iff the version transitioned out of ``unconfirmed`` this call.
    """
    factory = session_factory or AsyncSessionLocal
    _now = now or _utcnow()
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
            # Re-check under the lock: a concurrent sweep may have resolved it.
            if version.status != "promoted":
                return False
            # Backstop: a promotion recorded without a confirmation deadline (a
            # legacy promotion path that skipped arming it) would never time out
            # and would wedge the next version forever.  Arm it durably from
            # promoted_at so confirmation is always bounded and the artifact rows
            # created below inherit a real deadline.
            if (
                version.deploy_confirm_deadline is None
                and version.promoted_at is not None
            ):
                version.deploy_confirm_deadline = version.promoted_at + timedelta(
                    seconds=_DEPLOY_CONFIRM_WINDOW_SECONDS
                )
                session.add(version)
            (
                provider_rows,
                had_required_unconfirmed,
                mutated,
                backend_artifact,
            ) = await _refresh_release_artifact_rows(session, version, _now)

            if not provider_rows:
                return False

            if not had_required_unconfirmed:
                if mutated:
                    await session.commit()
                return False

            if backend_artifact is not None:
                if project_backend_revision_artifact(version, backend_artifact):
                    mutated = True
                session.add(version)

            aggregate_status, aggregate_reason = aggregate_required_artifact_status(
                provider_rows
            )
            # Undecidable corruption: a promotion with NO recorded promoted_sha
            # can never confirm by SHA-match — every required provider that needs
            # it returns "no decision" (the backend times out, but agent_image
            # bails on the missing SHA), so the aggregate sits "unconfirmed"
            # forever and wedges the next version (the real v7 case). Once the
            # (backstopped) confirmation window has elapsed, resolve it as a
            # TIMEOUT, not a permanent wedge and not a deploy_failed: nothing went
            # wrong, we simply cannot verify what shipped.
            if aggregate_status == "unconfirmed" and not version.promoted_sha:
                _deadline = _to_naive_utc(version.deploy_confirm_deadline)
                _now_naive = _to_naive_utc(_now) or _now
                if _deadline is not None and _now_naive >= _deadline:
                    aggregate_status, aggregate_reason = (
                        "confirmation_timed_out",
                        "promoted_sha_missing",
                    )
            if aggregate_status == "unconfirmed":
                if mutated:
                    await session.commit()
                return False

            if aggregate_status == "serving_confirmed":
                version.serving_status = "serving_confirmed"
                version.deploy_failure_reason = None
                session.add(version)
                await _record_post_promotion_outcome(
                    session, version, "serving_confirmed", _now
                )
                await session.commit()
                logger.info(
                    "[RELEASE_QUALIFIER] serving confirmed for promoted v%d "
                    "(served_sha=%s)",
                    version.version_number,
                    (version.served_sha or "")[:12],
                )
                return True

            if aggregate_status == "confirmation_timed_out":
                # The window elapsed without confirmation, but nothing was
                # observed going bad (no rollback/halt/sha-mismatch).  This is
                # NOT a failed deploy: surface a distinct, softer state so the
                # operator/strategist verify serving rather than treating it as
                # a crisis.  No loud deploy-failed ERROR alert; recoverable to
                # serving_confirmed if the SHA is later observed serving.
                version.serving_status = "confirmation_timed_out"
                version.deploy_failure_reason = aggregate_reason
                session.add(version)
                await _record_post_promotion_outcome(
                    session, version, "confirmation_timed_out", _now
                )
                try:
                    from app.strategic_trigger import request_strategic_refresh

                    await request_strategic_refresh(
                        session,
                        version.project_id,
                        f"promotion_confirmation_timed_out:{aggregate_reason}",
                    )
                except Exception:
                    logger.exception(
                        "[RELEASE_QUALIFIER] strategic refresh arm raised for "
                        "timed-out v%d (non-fatal)",
                        version.version_number,
                    )
                await session.commit()
                logger.warning(
                    "[RELEASE_QUALIFIER] serving confirmation TIMED OUT for "
                    "promoted v%d (reason=%s) — promoted with no active deploy "
                    "failure, but the promoted SHA could not be confirmed serving "
                    "within the window",
                    version.version_number,
                    aggregate_reason,
                )
                return True

            version.serving_status = "deploy_failed"
            version.deploy_failure_reason = aggregate_reason
            session.add(version)
            _aggregate_summary, _artifact_evidence = summarize_release_artifacts(
                version,
                [artifact for _provider, artifact in provider_rows],
                project=await session.get(Project, version.project_id),
            )
            await _record_post_promotion_outcome(
                session, version, "deploy_failed", _now
            )
            # Loud operator alert (ERROR), deduped via deploy_confirm_alerted_at.
            try:
                from app.release_stall_alerts import _alert_promotion_deploy_failed

                await _alert_promotion_deploy_failed(
                    session,
                    version,
                    artifact_evidence=_artifact_evidence,
                    aggregate_deploy_failure_reason=_aggregate_summary["reason_code"],
                )
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] deploy-failed alert raised for v%d "
                    "(non-fatal)",
                    version.version_number,
                )
            # Wake the strategist now instead of leaving it to discover the
            # failed deploy on its next natural cycle (gated behind the
            # strategic-spawn cooldown). A failed production deploy is the
            # loudest release signal, so arm the same coalescing refresh marker
            # the post-qualification path uses. Best-effort and non-fatal:
            # request_strategic_refresh swallows its own errors, does NOT commit
            # (rides this transaction), and the single-slot marker coalesces, so
            # arming can never abort the deploy-failed write and a re-scanned
            # version cannot re-fire (the serving_status!=unconfirmed guard above
            # means this branch runs exactly once per version).
            try:
                from app.strategic_trigger import request_strategic_refresh

                await request_strategic_refresh(
                    session,
                    version.project_id,
                    f"promotion_deploy_failed:{aggregate_reason}",
                )
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] strategic refresh arm raised for v%d "
                    "(non-fatal)",
                    version.version_number,
                )
            await session.commit()
            logger.error(
                "[RELEASE_QUALIFIER] promotion deploy FAILED for v%d "
                "(reason=%s) — promoted SHA never confirmed serving",
                version.version_number,
                aggregate_reason,
            )
            return True
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] serving-confirmation: unexpected error for "
                "version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] serving-confirmation: rollback failed "
                    "for version %s",
                    version_id,
                )
            return False


# ---------------------------------------------------------------------------
# Post-promotion deploy-failure recovery
# ---------------------------------------------------------------------------
#
# A ``serving_status="deploy_failed"`` latch is NOT terminal-by-construction.  A
# deploy that boot-crashed and then recovered, or that was merely slow to come up
# past the bounded confirmation window (the real v5 pattern: promoted 14:58 UTC,
# only began serving at 19:51 UTC), would otherwise be rendered permanently
# untrue across every surface — version status, the operator deploy-failed ERROR
# alert, the strategist "recover the failed deploy" crisis directive, and the
# QualificationVerdict.post_promotion_outcome precision sample.  When such a
# version is later observed to ACTUALLY serve its promoted SHA, the recovery path
# reconciles it back to a truthful serving_confirmed state and clears every
# derived failure surface — bounded by a recovery window so a genuinely dead
# deploy still stays loudly failed forever.
_DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS_DEFAULT = 24 * 3600


async def _recover_serving_confirmed(
    session,
    version: release_version.ReleaseVersion,
    served_sha: str,
    served_at: datetime | None,
    now: datetime,
    *,
    confirmed_reason_code: str | None = None,
) -> None:
    """Flip a deploy_failed version back to serving_confirmed and clear surfaces.

    Shared mutation helper for the deploy-failure recovery path.  Given a version
    whose promoted SHA is now INDEPENDENTLY observed serving, it:

      * flips ``serving_status`` ``deploy_failed`` -> ``serving_confirmed``,
      * stamps ``served_sha`` / ``served_at`` from the confirmed observation,
      * clears ``deploy_failure_reason``,
      * updates the ``QualificationVerdict.post_promotion_outcome`` to
        ``serving_confirmed`` (reusing ``_record_post_promotion_outcome``'s
        SAVEPOINT discipline so the immutable verdict history is appended-to, not
        mutated, and a verdict failure never rolls back the serving-status write),
      * resolves the open promotion-deploy-failed operator alert, and
      * arms a coalescing strategic refresh so the crisis directive recomputes
        against the now-true serving_confirmed state this cycle.

    ``confirmed_reason_code`` optionally annotates the confirmed backend artifact
    (e.g. ``superseded_by_descendant`` when the recovery re-check resolved a
    latched sha_mismatch as supersession); left ``None`` the confirmation clears
    the artifact reason code, exactly as before.

    Runs entirely on the caller's already-locked session; does NOT commit (the
    caller commits under the SAME per-version row lock).
    """
    backend_artifact, _created = await get_or_create_release_deploy_artifact(
        session,
        version,
        BACKEND_REVISION_ARTIFACT_KIND,
        confirmation_deadline=version.deploy_confirm_deadline,
    )
    apply_serving_decision(
        backend_artifact,
        ("serving_confirmed", served_sha, served_at, confirmed_reason_code),
        now=now,
    )
    session.add(backend_artifact)
    project_backend_revision_artifact(version, backend_artifact)
    version.deploy_failure_reason = None
    session.add(version)
    await _record_post_promotion_outcome(session, version, "serving_confirmed", now)
    # Resolve the loud deploy-failed operator alert — it no longer describes a
    # real condition once the promoted SHA is confirmed serving.  Best-effort and
    # non-fatal: a resolve failure must never abort the recovery write.
    try:
        from app.release_stall_alerts import _resolve_promotion_deploy_failed_alert

        await _resolve_promotion_deploy_failed_alert(session, version)
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] deploy-failed alert resolve raised for "
            "recovered v%d (non-fatal)",
            version.version_number,
        )
    # Wake the strategist so the "recover the failed deploy" crisis directive
    # recomputes against the now-true serving_confirmed state instead of waiting
    # for the next natural cycle.  Best-effort and non-fatal:
    # request_strategic_refresh swallows its own errors, does NOT commit (rides
    # this transaction), and the single-slot marker coalesces.
    try:
        from app.strategic_trigger import request_strategic_refresh

        await request_strategic_refresh(
            session,
            version.project_id,
            "promotion_deploy_recovered",
        )
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] strategic refresh arm raised for recovered "
            "v%d (non-fatal)",
            version.version_number,
        )


async def _reconcile_if_deploy_failed_recovered(
    version_id: uuid.UUID,
    *,
    session_factory=None,
    now: datetime | None = None,
    recovery_window_seconds: float = _DEPLOY_FAILED_RECOVERY_WINDOW_SECONDS_DEFAULT,
) -> bool:
    """Recover a ``deploy_failed`` version whose promoted SHA is now serving.

    For a ``promoted`` version latched to ``serving_status="deploy_failed"`` (or
    ``confirmation_timed_out``), re-run the full required-artifact truth recheck
    against the live DeploymentState.  When the promoted SHA is now INDEPENDENTLY
    observed serving — the backend_revision provider confirming an observed SHA
    that equals OR is an ancestry-verified descendant of ``promoted_sha`` AND the
    full required-artifact aggregate rechecks as ``serving_confirmed`` — flip it
    back to a truthful ``serving_confirmed`` state via ``_recover_serving_confirmed``
    and clear every derived failure surface.  Any other verdict (still failed,
    rolled-back, halted, determinate non-ancestry sha_mismatch, a still-failed
    required non-backend artifact, or an indeterminate check) leaves the version
    deploy_failed — only the artifact-row truth actually learned is committed.

    Recovery is gated on the deterministic positive, NOT on elapsed time: an
    ancestry-verified deploy that the descendant-aware provider now confirms
    serving recovers even when the old recovery window has long since elapsed,
    because the truth recheck — not a timer — decides.  The
    ``recovery_window_seconds`` parameter is retained for signature/back-compat
    with the sweep call site and tests but no longer short-circuits the recheck;
    every negative or indeterminate result stays latched ``deploy_failed`` by the
    aggregate gate, so an elapsed window can never force-clear a dead deploy.

    Runs in its own dedicated session under a row lock — the same per-version
    isolation pattern as ``_confirm_serving_or_fail`` — so a failure on one
    version cannot touch another's committed work.  Returns True iff the version
    recovered this call.
    """
    factory = session_factory or AsyncSessionLocal
    _now = now or _utcnow()
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
            # Re-check under the lock: a concurrent sweep may have resolved it.
            if version.status != "promoted":
                return False
            # Both an active failure (deploy_failed) and a confirmation timeout
            # (confirmation_timed_out) are non-terminal: if the promoted SHA is
            # later observed serving, recover either to serving_confirmed.
            if version.serving_status not in (
                "deploy_failed",
                "confirmation_timed_out",
            ):
                return False

            # Recovery is gated on the deterministic positive below (a full
            # required-artifact serving_confirmed aggregate, including the
            # descendant-aware backend_revision provider), NOT on elapsed time.
            # The ``recovery_window_seconds`` param is retained for back-compat
            # but intentionally no longer short-circuits this recheck: an
            # ancestry-verified deploy must be able to recover even beyond the
            # old window, while every negative/indeterminate result stays latched
            # deploy_failed via the aggregate gate.
            _ = recovery_window_seconds

            (
                provider_rows,
                _,
                mutated,
                backend_artifact,
            ) = await _refresh_release_artifact_rows(
                session,
                version,
                _now,
                recheck_failed_required=True,
            )
            aggregate_status, _ = aggregate_required_artifact_status(provider_rows)
            backend_is_serving = (
                backend_artifact is not None
                and backend_artifact.serving_status == "serving_confirmed"
                and backend_artifact.served_identifier is not None
            )
            # Recovery now means the FULL required-artifact registry has
            # reconfirmed, not merely the backend-revision artifact. Commit any
            # artifact-row truth we learned even when the aggregate version state
            # must remain deploy_failed.
            if aggregate_status != "serving_confirmed" or not backend_is_serving:
                if mutated:
                    await session.commit()
                return False
            await _recover_serving_confirmed(
                session,
                version,
                backend_artifact.served_identifier,
                backend_artifact.served_at,
                _now,
                # Preserve a supersession annotation set by the provider
                # re-check (a latched sha_mismatch resolved as
                # superseded_by_descendant); None for ordinary recoveries.
                confirmed_reason_code=backend_artifact.reason_code,
            )
            await session.commit()
            logger.info(
                "[RELEASE_QUALIFIER] deploy-failed RECOVERED for promoted v%d "
                "(served_sha=%s) — promoted SHA confirmed serving after a prior "
                "deploy_failed latch",
                version.version_number,
                (version.served_sha or "")[:12],
            )
            return True
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] deploy-failed recovery: unexpected error "
                "for version %s",
                version_id,
            )
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] deploy-failed recovery: rollback failed "
                    "for version %s",
                    version_id,
                )
            return False
