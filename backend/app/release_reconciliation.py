"""Reconciliation helpers for release qualification sweeps.

This module owns the out-of-band reconciliation paths that catch a release
version up when its promotion PR lands (or is terminally closed) outside the
normal qualification completion flow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select

from app import github, release_version
from app.database import AsyncSessionLocal
from app.models import (
    DeploymentState,
    DeploymentStatus,
    Project,
    QualificationVerdict,
)
from app.release_promotion import (
    _candidate_already_on_main,
    _idempotent_pr_url,
    _lts_branch_names,
    _record_promotion_failure,
    _record_promotion_success,
    _resolve_github_with_session,
)
from app.rollup_migration_validation import _validate_migration_chain_post_merge
from app.release_scoring import _CI_RED, _classify_check_runs
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
                alembic_status, alembic_reason = await _validate_migration_chain_post_merge(
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
                            if ci_state == _CI_RED:
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

            age_seconds = (_utcnow() - enabled_at).total_seconds()
            cap_seconds = _promotion_pending_timeout_seconds()
            if age_seconds < cap_seconds:
                return False

            age_hours = age_seconds / 3600.0
            pr_number = pending.get("pr_number")
            pr_url: str | None = pending.get("pr_url")
            # base_record drops promotion_pending (mirrors
            # _reconcile_if_pending_auto_merge_landed) so the recorded failure
            # CLEARS the sub-state and re-arms the qualification-timeout backstop.
            base_record = {k: v for k, v in qual.items() if k != "promotion_pending"}

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
            await _record_promotion_failure(
                session,
                version,
                base_record,
                "promotion_pending_timeout",
                detail=(
                    f"promotion PR #{pr_number} stranded in promotion_pending "
                    f"for {age_hours:.0f}h (> {cap_seconds / 3600.0:.0f}h cap) "
                    f"without a terminal merge/close/CI signal — force-failed so "
                    f"the version retries qualification"
                ),
                pr_url=pr_url,
                lock_released=False,
            )
            await session.commit()
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
      * ``("deploy_failed", reason)`` (third element ``None``) — the deploy did
        not come up healthy: a ROLLED_BACK/HALTED state arose after the promotion
        (``reason`` ``rolled_back``/``halted``), the observed serving SHA does
        not match the promoted SHA (``sha_mismatch``), no new healthy revision
        appeared by ``deadline`` (``no_new_revision``), or a new revision did go
        STABLE but its serving SHA could never be independently observed before
        the deadline (``serving_sha_unobservable``).
    """
    promoted_at = _to_naive_utc(promoted_at)
    deadline = _to_naive_utc(deadline)
    now = _to_naive_utc(now) or now

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

        # SERVING CONFIRMED: a new revision booted after the promotion and the
        # monitoring window passed (STABLE).  Confirm ONLY when the live serving
        # SHA was INDEPENDENTLY observed AND matches the promoted SHA — never
        # echo the promoted (merged) SHA as a stand-in.  When the serving SHA is
        # unobservable, do NOT confirm: keep waiting within the window, and let
        # the deadline land a distinct ``serving_sha_unobservable`` failure.
        if status == DeploymentStatus.STABLE and revision_after_promotion:
            current_sha = ds.current_sha or None
            if current_sha and promoted_sha:
                if current_sha == promoted_sha:
                    return ("serving_confirmed", current_sha, deployed_at)
                return ("deploy_failed", "sha_mismatch", None)
            # New revision is STABLE but the live serving SHA (or the promoted
            # SHA to match it against) cannot be observed — not confirmable.
            serving_sha_unobservable = True

    # No confirmation yet — deploy-failed only once the bounded window elapses.
    if deadline is not None and now >= deadline:
        if serving_sha_unobservable:
            return ("deploy_failed", "serving_sha_unobservable", None)
        return ("deploy_failed", "no_new_revision", None)
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
            if version.serving_status != "unconfirmed":
                return False

            ds = await session.get(DeploymentState, 1)
            outcome = _classify_serving_outcome(
                ds,
                version.promoted_at,
                version.promoted_sha,
                version.deploy_confirm_deadline,
                _now,
            )
            if outcome is None:
                return False

            kind, sha_or_reason, served_at = outcome
            if kind == "serving_confirmed":
                version.serving_status = "serving_confirmed"
                version.served_sha = sha_or_reason
                version.served_at = served_at or _now
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

            # kind == "deploy_failed"
            version.serving_status = "deploy_failed"
            version.deploy_failure_reason = sha_or_reason
            session.add(version)
            await _record_post_promotion_outcome(
                session, version, "deploy_failed", _now
            )
            # Loud operator alert (ERROR), deduped via deploy_confirm_alerted_at.
            try:
                from app.release_stall_alerts import _alert_promotion_deploy_failed

                await _alert_promotion_deploy_failed(session, version)
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
                    f"promotion_deploy_failed:{sha_or_reason}",
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
                sha_or_reason,
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

    Runs entirely on the caller's already-locked session; does NOT commit (the
    caller commits under the SAME per-version row lock).
    """
    version.serving_status = "serving_confirmed"
    version.served_sha = served_sha
    version.served_at = served_at or now
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

    For a ``promoted`` version latched to ``serving_status="deploy_failed"`` still
    inside the recovery window, re-run the pure ``_classify_serving_outcome``
    against the live DeploymentState.  When the promoted SHA is now INDEPENDENTLY
    observed serving (a STABLE new revision booted after ``promoted_at`` whose
    live ``current_sha`` matches ``promoted_sha``) flip it back to a truthful
    ``serving_confirmed`` state via ``_recover_serving_confirmed`` and clear every
    derived failure surface.  Any other classifier verdict (still failed,
    rolled-back, sha-mismatch, or no decision) leaves the version deploy_failed.

    Beyond the recovery window — anchored on when the failure was recorded
    (``deploy_confirm_alerted_at``, falling back to ``deploy_confirm_deadline`` /
    ``promoted_at``) — a genuinely dead deploy is no longer re-checked and stays
    failed forever.

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
            if version.serving_status != "deploy_failed":
                return False

            # Bound recovery: beyond the window a genuinely dead deploy stays
            # loudly failed forever.  Anchor on when the failure was recorded.
            anchor = _to_naive_utc(
                version.deploy_confirm_alerted_at
                or version.deploy_confirm_deadline
                or version.promoted_at
            )
            now_naive = _to_naive_utc(_now) or _now
            if (
                anchor is not None
                and recovery_window_seconds > 0
                and (now_naive - anchor).total_seconds() > recovery_window_seconds
            ):
                return False

            ds = await session.get(DeploymentState, 1)
            outcome = _classify_serving_outcome(
                ds,
                version.promoted_at,
                version.promoted_sha,
                version.deploy_confirm_deadline,
                _now,
            )
            # Only an independently-observed serving confirmation recovers the
            # version; never echo promoted_sha as a stand-in (the classifier
            # already enforces current_sha == promoted_sha for this verdict).
            if outcome is None or outcome[0] != "serving_confirmed":
                return False
            _kind, served_sha, served_at = outcome
            await _recover_serving_confirmed(
                session, version, served_sha, served_at, _now
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
