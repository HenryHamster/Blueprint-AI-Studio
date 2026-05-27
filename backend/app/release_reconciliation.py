"""Reconciliation helpers for release qualification sweeps.

This module owns the out-of-band reconciliation paths that catch a release
version up when its promotion PR lands (or is terminally closed) outside the
normal qualification completion flow.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app import github, release_version
from app.database import AsyncSessionLocal
from app.models import Project
from app.release_promotion import (
    _candidate_already_on_main,
    _idempotent_pr_url,
    _lts_branch_names,
    _record_promotion_failure,
    _record_promotion_success,
    _resolve_github_with_session,
)
from app.release_scoring import _CI_RED, _classify_check_runs

logger = logging.getLogger(__name__)


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
