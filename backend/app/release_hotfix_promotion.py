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

from app import github, hotfix_narrow_candidate, hotfix_policy, release_version
from app.database import AsyncSessionLocal
from app.failure_reason import exception_reason
# Moved out to keep this module inside its governance cap; re-exported so
# ``release_qualifier.check_hotfix_scope_preview`` keeps resolving unchanged.
from app.hotfix_narrow_candidate import (  # noqa: F401
    check_hotfix_scope_preview as check_hotfix_scope_preview,
)
from app.models import Project
from app.time_utils import _utcnow
_MODULE_SIZE_GOVERNANCE_CAP: int = 537

logger = logging.getLogger(__name__)


def _release_qualifier():
    """Resolve the qualification engine only after both modules initialize."""
    from app import release_qualifier

    return release_qualifier


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
# candidate merged outright (``promoted``) or a promotion PR was opened under
# ``pending_managed_check_wait`` (the CI-gated primary flow). Each skipped the
# audit/scoring gate and is owed normal-gate re-qualification, so the audit
# trail (completion-debt backfill + operator Alert) MUST be recorded for every
# one of them, not only the already-merged ``promoted`` case. A
# ``promotion_failed`` / no-op result deployed nothing, so it owes nothing.
_HOTFIX_PROMOTE_ATTEMPTED_STATUSES: frozenset[str] = frozenset(
    {"promoted", "pending_managed_check_wait"}
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
    repair_commit_shas: list[str] | None = None,
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
    _rq = _release_qualifier()
    _promo = _rq._promotion_pending_result(version, latest_verdict=_latest_verdict)
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
        # Narrow lane only: the operator-named repair commits this candidate was
        # materialized from (empty when the whole candidate diff was shipped).
        "hotfix_promote_repair_commit_shas": list(repair_commit_shas or []),
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
    repair_commit_shas: list[str] | None = None,
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
                repair_commit_shas,
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

    _rq = _release_qualifier()
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
    repair_commit_shas: list[str] | None = None,
    record_debt: bool = True,
    record_alert: bool = True,
) -> None:
    """Durable, append-only audit trail for a hotfix promote that took effect.

    Recorded for every status in ``_HOTFIX_PROMOTE_ATTEMPTED_STATUSES`` — the
    already-merged ``promoted`` case and the CI-gated
    ``pending_managed_check_wait`` case where the promotion PR is open but not
    yet landed. Both bypassed the audit gate and are equally owed re-qualification.

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
        # WHICH commits the operator asserted were the repair (narrow lane);
        # ``diff_file_list`` above is WHICH files actually shipped.
        "repair_commit_shas": list(repair_commit_shas or []),
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
                    event_type="release.promotion_deploy_failed",
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
    repair_commit_shas: list[str] | None = None,
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

    ``repair_commit_shas`` selects the NARROW lane (knowledge 0cee18f3): instead
    of scope-checking (and shipping) everything integration accumulated since the
    last promotion — which in a real latch is hundreds of product files and is
    therefore ALWAYS rejected — the operator names the commits that constitute
    the repair.  Those commits' OWN diffs are scope-checked by the SAME
    fail-closed predicate, and an eligible repair is materialized as a new commit
    on top of the promotion target carrying only those files.  The materialized
    SHA is then scope-checked AGAIN through the ordinary compare below, so
    authorization still derives from the actual diff of what ships.  CI is
    resolved against the CONTENT source (the repair commit that was actually
    built and tested); the promotion PR then runs its own CI on the narrow branch.
    """
    version = await release_version.current_open_version(session, project_id)
    if version is None:
        return {"status": "no_open_version"}
    project = await session.get(Project, project_id)
    if project is None:
        return {"status": "project_not_found"}
    _rq = _release_qualifier()
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
            return {"status": "sha_error", "detail": exception_reason(exc)}
    if not candidate_sha:
        return {"status": "sha_error", "detail": "empty candidate sha"}
    # Same normalization as force-promote: an operator-typed abbreviated sha is
    # resolved to its canonical 40-hex form here, at the single server-side
    # entry point, instead of dying inside GitHub's ref-create API with a bare
    # 422 several layers deeper (incident 2026-08-23).  Full sha ⇒ no network.
    try:
        candidate_sha = await github.resolve_commit_sha(
            token, owner, repo, candidate_sha, project_id=str(project_id)
        )
    except Exception as exc:  # noqa: BLE001 — an unresolvable ref IS an operator error
        return {"status": "sha_error", "detail": exception_reason(exc)}
    # The prior deployed SHA is BOTH the diff base and an audit-trail field.
    try:
        prior_deployed_sha = await github.get_branch_sha(
            token, owner, repo, promotion_target, live=True
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "sha_error", "detail": exception_reason(exc)}
    if not prior_deployed_sha:
        return {"status": "sha_error", "detail": "empty promotion-target sha"}

    # NARROW lane: materialize a machinery-only candidate from the named repair
    # commits before the ordinary compare below re-verifies what will ship.
    ci_ref = candidate_sha
    _repair_shas = list(repair_commit_shas or [])
    if _repair_shas:
        narrow = await hotfix_narrow_candidate.materialize_narrow_hotfix_candidate(
            token,
            owner,
            repo,
            prior_deployed_sha=prior_deployed_sha,
            repair_commit_shas=_repair_shas,
            content_ref=candidate_sha,
            project_id=str(project_id),
        )
        if narrow.get("status") != "materialized":
            logger.warning(
                "[RELEASE_QUALIFIER] HOTFIX-PROMOTE narrow lane REFUSED: project %s "
                "repair=%s status=%s — %s",
                project_id,
                [sha[:8] for sha in _repair_shas],
                narrow.get("status"),
                narrow.get("reason"),
            )
            return narrow
        # CI ran on the repair commit's own tree, never on the just-created
        # narrow object; resolve it against that content source.
        ci_ref = narrow["content_ref"]
        candidate_sha = narrow["candidate_sha"]

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
    ci_green, ci_resolution = await _rq._resolve_ci_at_finalize(session, version, ci_ref)
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
        repair_commit_shas=_repair_shas,
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
            repair_commit_shas=_repair_shas,
        )
    return result
