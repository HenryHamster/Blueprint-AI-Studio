"""Narrow operator-approved release-machinery hotfix promotion channel.

Separated from the qualification engine to keep both responsibilities bounded.
The stable public names are re-exported by :mod:`app.release_qualifier`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.release_qualifier as _rq  # module-level ref — breaks circular import
from app import github, hotfix_policy, release_version
from app.database import AsyncSessionLocal
from app.models import Project
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# ── Operator hotfix-deploy channel (design d-ae60ffe3, knowledge f69ffbff) ────
# A NARROW, OWNER-approved, fail-closed sibling of force-promote: it fast-tracks
# a release-machinery fix ahead of the normal qualification gate, but — unlike
# force-promote, which ships the WHOLE candidate tree — it may ONLY ship the
# allowlisted release-machinery files (backend/app/hotfix_policy.py). The diff
# allowlist is computed from the candidate SHA's ACTUAL changed files, never from
# the operator's declared scope_modules, and it fails closed on anything
# unexpected. Like force-promote it skips the AUDIT/scoring gate ONLY — not CI,
# not promote()'s merge-time migration-chain backstop.


# Statuses from ``promote()`` where the hotfix bypass ACTUALLY took effect: the
# candidate merged outright (``promoted``) or a promotion PR was opened with
# auto-merge / a managed-check wait armed (``pending_auto_merge`` /
# ``pending_managed_check_wait`` — the CI-gated primary flow). Each skipped the
# audit/scoring gate and is owed normal-gate re-qualification, so the audit
# trail (completion-debt backfill + operator Alert) MUST be recorded for every
# one of them, not only the already-merged ``promoted`` case. A
# ``promotion_failed`` / no-op result deployed nothing, so it owes nothing.
_HOTFIX_PROMOTE_ATTEMPTED_STATUSES: frozenset[str] = frozenset(
    {"promoted", "pending_auto_merge", "pending_managed_check_wait"}
)


async def _hotfix_promote_locked(
    session: AsyncSession,
    version_id: uuid.UUID,
    candidate_sha: str,
    candidate_diff_hash: str | None,
    actor: str | None,
    reason: str | None,
    scope_modules: list[str] | None,
    diff_file_list: list[str],
    prior_deployed_sha: str | None,
) -> dict[str, Any]:
    """Lock + HOTFIX-promote an open version on a dedicated session (no audit).

    Body of ``_hotfix_promote_in_dedicated_session``.  Mirrors
    ``_force_promote_locked`` invariant-for-invariant — same FOR UPDATE lock, same
    open/in-flight refusals, same commit discipline — but marks the base_record
    with ``hotfix_promote`` and carries the operator's actor/reason/scope_modules
    justification instead of the forced_promote_* provenance.  The scope check has
    ALREADY passed (pre-lock, in ``hotfix_promote_current_version``); this body
    only performs the audited promote.
    """
    version = (
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
    if version is None:
        return {"status": "version_not_found"}
    if version.status != "open":
        return {"status": "not_open", "version_status": version.status}
    from app.qualification_state_resolver import resolve_newest_qualification_verdict
    _latest_verdict = await resolve_newest_qualification_verdict(session, version)
    _promo = _rq._promotion_in_flight_result(version, latest_verdict=_latest_verdict)
    if _promo is not None:
        return _promo
    now = _utcnow()
    _scope_modules = list(scope_modules or [])
    # Hotfix composite: NO audit score (like force-promote); promote() only reads
    # ``confidence`` for the PR body, so omit it.
    composite: dict[str, Any] = {"promote": True, "hotfix_promote": True}
    base_record: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "candidate_diff_hash": candidate_diff_hash,
        "confidence": None,
        "hotfix_promote": True,
        "hotfix_promote_by": actor,
        "hotfix_promote_at": now.isoformat(),
        "hotfix_promote_reason": reason,
        "hotfix_promote_scope_modules": _scope_modules,
        # These are committed with the bypass itself so the sweep can recreate
        # its promised audit trail if the request session dies after promotion.
        "hotfix_promote_diff_file_list": list(diff_file_list),
        "hotfix_promote_prior_deployed_sha": prior_deployed_sha,
        "started_at": now.isoformat(),
        "finalized_at": now.isoformat(),
    }
    logger.warning(
        "[RELEASE_QUALIFIER] HOTFIX-PROMOTE: v%d cutting release branch + opening "
        "promotion PR at candidate %s (audit gate SKIPPED; CI + merge-time "
        "migration guard still apply) — actor=%s reason=%s scope_modules=%s",
        version.version_number,
        candidate_sha[:8],
        actor,
        reason,
        _scope_modules,
    )
    result = await _rq.promote(
        session,
        version,
        candidate_sha,
        composite=composite,
        base_record=base_record,
    )
    await session.commit()
    return result


async def _hotfix_promote_in_dedicated_session(
    version_id: uuid.UUID,
    candidate_sha: str,
    candidate_diff_hash: str | None,
    actor: str | None,
    *,
    reason: str | None,
    scope_modules: list[str] | None,
    diff_file_list: list[str],
    prior_deployed_sha: str | None,
    session_factory=None,
) -> dict[str, Any]:
    """Run HOTFIX-promote on a DEDICATED session (mirrors the force-promote path).

    A dedicated ``AsyncSessionLocal`` is required because ``promote()`` issues an
    intermediate lock-releasing commit.  Runs the same post-commit reconcile /
    strategic hooks the force/finalize paths run, but only when a promotion
    actually attempted.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            result = await _hotfix_promote_locked(
                session,
                version_id,
                candidate_sha,
                candidate_diff_hash,
                actor,
                reason,
                scope_modules,
                diff_file_list,
                prior_deployed_sha,
            )
        except Exception:
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[RELEASE_QUALIFIER] hotfix-promote: rollback failed for "
                    "version %s",
                    version_id,
                )
            raise

    if result.get("status") not in _rq._FORCE_PROMOTE_NOOP_STATUSES:
        await _rq._maybe_reconcile_after_promotion_conflict(
            version_id, result, session_factory=session_factory
        )
        await _rq._maybe_trigger_strategic_after_qualification(
            version_id, result, session_factory=session_factory
        )
    return result


async def _record_hotfix_audit_trail(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    candidate_sha: str,
    actor: str | None,
    reason: str | None,
    scope_modules: list[str] | None,
    diff_file_list: list[str] | None,
    prior_deployed_sha: str | None,
    record_debt: bool = True,
    record_alert: bool = True,
) -> None:
    """Durable, append-only audit trail for a hotfix promote that took effect.

    Recorded for every status in ``_HOTFIX_PROMOTE_ATTEMPTED_STATUSES`` — the
    already-merged ``promoted`` case AND the CI-gated ``pending_auto_merge`` /
    ``pending_managed_check_wait`` cases where the promotion PR is open with
    auto-merge armed but not yet landed. All three bypassed the audit gate and
    are equally owed re-qualification.

    Reuses existing primitives (design §2.3 prefers this over a new table):

      * a ``hotfix_deploy_pending_backfill`` :class:`CompletionDebtEntry` — the
        bypass is owed follow-through, so it stays open (contributing to the
        debt-pressure sub-score) until the NORMAL gate re-qualifies the same
        content; and
      * a WARNING :class:`Alert` so the operator sees the hotfix-deploy event.

    Best-effort: the completion-debt recorder never raises, and a failure to add
    the Alert must not undo the (already-committed) promotion.  Both are staged
    on the request ``session``; the caller commits.
    """
    from app.completion_debt import (
        KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
        arecord_completion_debt,
        hotfix_deploy_alert_dedupe_key,
        hotfix_deploy_debt_dedupe_key,
    )
    from app.models import Alert, AlertSeverity

    _scope_modules = list(scope_modules or [])
    _diff_files = list(diff_file_list or [])
    detail = {
        "candidate_sha": candidate_sha,
        "actor": str(actor) if actor is not None else None,
        "reason": reason,
        "scope_modules": _scope_modules,
        "diff_file_list": _diff_files,
        "prior_deployed_sha": prior_deployed_sha,
    }
    if record_debt:
        await arecord_completion_debt(
            session,
            dedupe_key=hotfix_deploy_debt_dedupe_key(project_id, candidate_sha),
            kind=KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
            title=(
                f"Hotfix-deploy of release machinery at {candidate_sha[:8]} owes "
                "normal-gate re-qualification"
            ),
            project_id=project_id,
            source="hotfix_promote",
            detail=detail,
        )
    if not record_alert:
        return
    try:
        alert_key = hotfix_deploy_alert_dedupe_key(project_id, candidate_sha)
        existing_alert = (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.dedupe_key == alert_key,
                )
            )
        ).scalar_one_or_none()
        if existing_alert is None:
            session.add(
                Alert(
                    project_id=project_id,
                    severity=AlertSeverity.WARNING,
                    title=f"Hotfix-deploy: release machinery fast-tracked at {candidate_sha[:8]}",
                    message=(
                        f"An OWNER hotfix-deploy fast-tracked candidate {candidate_sha[:8]} "
                        "(release machinery only) ahead of the normal qualification gate. "
                        f"Actor={actor} reason={reason!r}. This bypass is owed re-qualification "
                        "by the normal gate (tracked as open completion debt)."
                    ),
                    source="hotfix_promote",
                    dedupe_key=alert_key,
                )
            )
    except Exception:  # noqa: BLE001 — an Alert failure must not undo the promotion
        logger.warning(
            "[RELEASE_QUALIFIER] hotfix-promote: failed to stage operator Alert "
            "for project %s candidate %s",
            project_id,
            candidate_sha[:8],
            exc_info=True,
        )


async def hotfix_promote_current_version(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    candidate_sha: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
    scope_modules: list[str] | None = None,
    session_factory=None,
) -> dict[str, Any]:
    """Operator HOTFIX-PROMOTE the project's open version — a NARROW force-promote.

    Fast-tracks a release-machinery fix ahead of the normal qualification gate,
    but — unlike ``force_promote_current_version`` — only when the candidate's
    ACTUAL changed files are all allowlisted release machinery
    (:mod:`app.hotfix_policy`).  Skips the AUDIT/scoring gate ONLY; CI and
    promote()'s merge-time migration-chain backstop still gate the merge.

    Resolves the candidate (explicit SHA else integration-branch HEAD) and the
    prior deployed SHA (promotion-target HEAD) + credentials on the request
    session, computes the candidate's changed files relative to the promotion
    target via a GitHub compare, and runs the FAIL-CLOSED scope check.  On a
    scope rejection it returns ``{"status": "scope_rejected", ...}`` WITHOUT
    cutting any branch.  On success it re-enters ``promote()`` in a DEDICATED
    session and records a durable audit trail (completion debt + Alert).
    """
    version = await release_version.current_open_version(session, project_id)
    if version is None:
        return {"status": "no_open_version"}
    project = await session.get(Project, project_id)
    if project is None:
        return {"status": "project_not_found"}
    resolved = await _rq._resolve_github_with_session(session, project)
    if resolved is None:
        return {"status": "no_credentials"}
    token, owner, repo = resolved
    integration_branch, promotion_target = _rq._lts_branch_names(project)
    if not candidate_sha:
        try:
            candidate_sha = await github.get_branch_sha(
                token, owner, repo, integration_branch, live=True
            )
        except Exception as exc:  # noqa: BLE001 — a missing branch / API error is recoverable
            return {"status": "sha_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if not candidate_sha:
        return {"status": "sha_error", "detail": "empty candidate sha"}
    # The prior deployed SHA is BOTH the diff base and an audit-trail field.
    try:
        prior_deployed_sha = await github.get_branch_sha(
            token, owner, repo, promotion_target, live=True
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "sha_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if not prior_deployed_sha:
        return {"status": "sha_error", "detail": "empty promotion-target sha"}

    # Compute the candidate's ACTUAL changed files (a GitHub compare) — the WHOLE
    # safety of the lane.  NEVER trust the operator-declared scope_modules as the
    # authorization source; it is only cross-checked/audited.
    from app import github_refs

    changed = await github_refs.get_changed_files_with_patches_between_refs(
        token, owner, repo, prior_deployed_sha, candidate_sha, project_id=str(project_id)
    )
    if changed is None:
        # Fail-closed: an indeterminate diff is not an empty change set.
        return {
            "status": "scope_rejected",
            "offending_files": [],
            "reason": (
                "could not resolve the candidate diff against the promotion target "
                "(GitHub compare failed) — refusing fail-closed"
            ),
        }
    changed_files = [f["filename"] for f in changed]
    main_py_patch = next(
        (f.get("patch") for f in changed if f["filename"] == hotfix_policy.MAIN_PY_PATH),
        None,
    )
    verdict = hotfix_policy.evaluate_hotfix_scope(
        changed_files, main_py_patch=main_py_patch
    )
    if not verdict.eligible:
        logger.warning(
            "[RELEASE_QUALIFIER] HOTFIX-PROMOTE scope REJECTED: project %s candidate "
            "%s actor=%s — %s (offending=%s)",
            project_id,
            candidate_sha[:8],
            actor,
            verdict.reason,
            verdict.offending_files,
        )
        return {
            "status": "scope_rejected",
            "offending_files": verdict.offending_files,
            "reason": verdict.reason,
        }

    candidate_diff_hash = _rq.compute_audited_surface_hash(
        await github.list_repo_tree_paths_at_ref(
            token, owner, repo, candidate_sha, project_id=str(project_id)
        )
    )
    # Explicitly resolve CI before opening the PR — the bypass skips AUDIT only.
    ci_green, ci_resolution = await _rq._resolve_ci_at_finalize(session, version, candidate_sha)
    ci_required = _rq._ci_required_for_finalize(project, ci_resolution)
    if ci_required and not ci_green:
        logger.warning(
            "[RELEASE_QUALIFIER] HOTFIX-PROMOTE blocked: project %s v%d candidate %s "
            "CI is not green (state=%s) — actor=%s reason=%s",
            project_id, version.version_number, candidate_sha[:8],
            ci_resolution.get("state"), actor, reason,
        )
        return {"status": "ci_not_green", "ci_resolution": ci_resolution}
    logger.warning(
        "[RELEASE_QUALIFIER] HOTFIX-PROMOTE requested: project %s v%d candidate %s "
        "actor=%s reason=%s scope_modules=%s changed=%s — SKIPPING the audit/scoring "
        "gate (CI + merge-time migration guard still apply)",
        project_id,
        version.version_number,
        candidate_sha[:8],
        actor,
        reason,
        list(scope_modules or []),
        changed_files,
    )
    result = await _hotfix_promote_in_dedicated_session(
        version.id,
        candidate_sha,
        candidate_diff_hash,
        actor,
        reason=reason,
        scope_modules=scope_modules,
        diff_file_list=changed_files,
        prior_deployed_sha=prior_deployed_sha,
        session_factory=session_factory,
    )
    if result.get("status") in _HOTFIX_PROMOTE_ATTEMPTED_STATUSES:
        await _record_hotfix_audit_trail(
            session,
            project_id,
            candidate_sha=candidate_sha,
            actor=actor,
            reason=reason,
            scope_modules=scope_modules,
            diff_file_list=changed_files,
            prior_deployed_sha=prior_deployed_sha,
        )
    return result


async def check_hotfix_scope_preview(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    candidate_sha: str | None = None,
) -> dict[str, Any]:
    """Read-only preview: report whether the current open version's candidate diff
    is hotfix-eligible WITHOUT promoting.

    Returns a dict with keys:
      ``status``         — one of 'eligible', 'ineligible', 'no_open_version',
                           'no_credentials', 'sha_error', 'diff_unavailable'
      ``eligible``       — bool (present when status is 'eligible' or 'ineligible')
      ``changed_files``  — list of all changed file paths (present on success)
      ``offending_files``— list of paths that failed the scope check (empty if eligible)
      ``reason``         — human-readable verdict string (present on success)
      ``candidate_sha``  — resolved candidate SHA (present on success)
      ``prior_deployed_sha`` — promotion-target HEAD SHA (present on success)

    This shares the credential-resolution and diff-fetch logic with
    ``hotfix_promote_current_version`` but makes NO mutations: no branch is cut,
    no PR is opened, no audit trail is written.
    """
    version = await release_version.current_open_version(session, project_id)
    if version is None:
        return {"status": "no_open_version"}
    project = await session.get(Project, project_id)
    if project is None:
        return {"status": "no_open_version"}
    resolved = await _rq._resolve_github_with_session(session, project)
    if resolved is None:
        return {"status": "no_credentials"}
    token, owner, repo = resolved
    integration_branch, promotion_target = _rq._lts_branch_names(project)
    if not candidate_sha:
        try:
            candidate_sha = await github.get_branch_sha(
                token, owner, repo, integration_branch, live=True
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "sha_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if not candidate_sha:
        return {"status": "sha_error", "detail": "empty candidate sha"}
    try:
        prior_deployed_sha = await github.get_branch_sha(
            token, owner, repo, promotion_target, live=True
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "sha_error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
    if not prior_deployed_sha:
        return {"status": "sha_error", "detail": "empty promotion-target sha"}

    from app import github_refs

    changed = await github_refs.get_changed_files_with_patches_between_refs(
        token, owner, repo, prior_deployed_sha, candidate_sha, project_id=str(project_id)
    )
    if changed is None:
        return {
            "status": "diff_unavailable",
            "eligible": False,
            "changed_files": [],
            "offending_files": [],
            "reason": (
                "could not resolve the candidate diff against the promotion target "
                "(GitHub compare failed)"
            ),
            "candidate_sha": candidate_sha,
            "prior_deployed_sha": prior_deployed_sha,
        }
    changed_files = [f["filename"] for f in changed]
    main_py_patch = next(
        (f.get("patch") for f in changed if f["filename"] == hotfix_policy.MAIN_PY_PATH),
        None,
    )
    verdict = hotfix_policy.evaluate_hotfix_scope(
        changed_files, main_py_patch=main_py_patch
    )
    return {
        "status": "eligible" if verdict.eligible else "ineligible",
        "eligible": verdict.eligible,
        "changed_files": changed_files,
        "offending_files": verdict.offending_files,
        "reason": verdict.reason,
        "candidate_sha": candidate_sha,
        "prior_deployed_sha": prior_deployed_sha,
    }
