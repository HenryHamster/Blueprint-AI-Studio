"""Promotion machinery — open + merge the release-branch → main PR.

Extracted from release_qualifier.py.  This module owns the ~1 200-line cluster
of 8 functions that handle the automain → main promotion step:

  _candidate_already_on_main   — idempotency pre-check (is candidate on main?)
  _promotion_pr_landed         — disambiguate merged=False results
  _idempotent_pr_url           — resolve the pr_url on idempotent success paths
  _relock_version_for_promotion_write — re-acquire the FOR UPDATE lock safely
  promote                      — the main entry point; creates branch, opens PR,
                                 merges, records result
  _delete_release_branch_best_effort — clean up the release-vN branch after merge
  _record_promotion_failure    — persist a failed promotion, keep version open
  _record_promotion_success    — persist a successful promotion, open next version

Also contains helpers that the promotion functions require but that must remain
importable from release_qualifier.py for back-compat:

  _lts_branch_names            — (integration_branch, promotion_target) tuple
  _resolve_github_with_session — resolve (token, owner, repo) for a project
  _notify_operator             — best-effort operator ProjectMessage posting
  _parse_iso_dt                — ISO-8601 string → datetime or None
  _record_qualification_verdict — write a QualificationVerdict history row

release_qualifier.py re-imports all of these so the public API surface is
unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import github, release_version
from app.models import Project, QualificationVerdict
from app.notification_dispatch import post_event_message
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
    get_project_promotion_target as _get_project_promotion_target,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Branch-name helpers
# ---------------------------------------------------------------------------


def _lts_branch_names(project=None) -> tuple[str, str]:
    """Return (integration_branch, promotion_target).

    integration_branch — the trunk the pipeline builds ON (default "main").
    promotion_target   — the LTS/deploy branch promotions land on (default "main").
    LTS model is considered active when these two differ.

    Both values resolve per-project when available, otherwise fall back to "main".
    """
    return (
        _get_project_integration_branch(project),
        _get_project_promotion_target(project),
    )


# ---------------------------------------------------------------------------
# GitHub credential resolution
# ---------------------------------------------------------------------------


async def _resolve_github_with_session(
    session: AsyncSession,
    project: Project,
) -> tuple[str, str, str] | None:
    """Resolve ``(token, owner, repo)`` using the active session for fallbacks.

    Same resolution order as ``get_github_token_for_task``: App-level discovery,
    then the user's stored installation id, then their PAT.  The session is
    needed only for the latter two (they read ``UserIntegration``).
    """
    owner = project.github_owner
    repo = project.github_repo
    if not owner or not repo:
        logger.warning(
            "[RELEASE_QUALIFIER] project %s has no authorized GitHub repo",
            project.id,
        )
        return None

    token: str | None = None
    try:
        token = await github.get_repo_installation_token(owner, repo)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[RELEASE_QUALIFIER] App-level token discovery failed for %s/%s",
            owner,
            repo,
        )

    if not token and project.user_id:
        from app.github import generate_installation_access_token
        from app.models import UserIntegration
        from app.secrets_manager import get_user_secret

        integration = (
            (
                await session.execute(
                    select(UserIntegration).where(
                        UserIntegration.user_id == project.user_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if integration and integration.github_installation_id:
            try:
                token = await generate_installation_access_token(
                    integration.github_installation_id, repositories=[repo]
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[RELEASE_QUALIFIER] stored installation token failed for %s/%s",
                    owner,
                    repo,
                )
        if not token and integration and integration.github_pat:
            try:
                token = await get_user_secret(integration.github_pat)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[RELEASE_QUALIFIER] PAT retrieval failed for %s/%s",
                    owner,
                    repo,
                )

    if not token:
        logger.warning(
            "[RELEASE_QUALIFIER] no GitHub token available for %s/%s",
            owner,
            repo,
        )
        return None
    return token, owner, repo


# ---------------------------------------------------------------------------
# Operator notification helper
# ---------------------------------------------------------------------------


async def _notify_operator(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    event_type: str,
    summary: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Best-effort operator notification for a release-qualifier verdict.

    Posts a system ``ProjectMessage`` so the operator sees every promotion
    outcome without polling.  Does not commit — the message rides the caller's
    commit, the same one that persists ``last_qualification``.

    A messaging failure must NEVER abort or fail qualification/promotion: the
    verdict is already decided and recorded by the time this runs.  Mirrors the
    best-effort ``create_tag`` pattern — wrapped in try/except, logs a warning
    and never propagates.
    """
    try:
        context: dict[str, Any] = {
            "version_number": version.version_number,
            "version_id": str(version.id),
        }
        if extra:
            context.update(extra)
        await post_event_message(
            session,
            version.project_id,
            event_type,
            summary,
            context=context,
        )
    except Exception as exc:  # noqa: BLE001 — notifications must never break a release
        logger.warning(
            "[RELEASE_QUALIFIER] failed to post operator notification %s "
            "(non-fatal): %s",
            event_type,
            exc,
        )


# ---------------------------------------------------------------------------
# Qualification verdict helpers
# ---------------------------------------------------------------------------


def _parse_iso_dt(raw: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a datetime, or None.

    Used to coerce the ``started_at`` / ``finalized_at`` ISO strings carried in
    a qualification ``base_record`` into datetime columns on the verdict-history
    row.  Returns None on a missing / non-string / unparseable value — recording
    is best-effort, so a bad timestamp must never raise.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _record_qualification_verdict(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    base_record: dict[str, Any],
    *,
    terminal_status: str,
    would_promote: bool,
    promotion_enabled: bool,
) -> None:
    """Best-effort: append ONE immutable ``QualificationVerdict`` history row.

    Phase 4 (calibration) durable evidence trail.  ``last_qualification`` is
    OVERWRITTEN on every finalize, so this appends a never-overwritten per-verdict
    row that ``compute_promotion_readiness`` reads across versions to reconstruct
    the dry-run track record.  Purely additive — ``last_qualification`` is still
    written exactly as before; this does not touch it.

    CRITICAL: recording must NEVER break or roll back a qualification/promotion.
    The verdict INSERT runs inside a SAVEPOINT (``begin_nested``) wrapped in
    try/except.  Before opening the savepoint the caller's already-staged
    mutations (``last_qualification`` etc.) are flushed into the OUTER
    transaction, so the savepoint contains ONLY the verdict INSERT — a rollback
    of that savepoint can never drop the qualification's own write.  Any failure
    (a malformed row, or — until the sibling Alembic migration lands — a missing
    ``qualificationverdict`` table) surfaces inside the savepoint, is rolled back
    to the savepoint leaving the outer transaction intact, and is logged and
    swallowed.  Purely additive: ``last_qualification`` is written exactly as
    before and the surrounding qualification/promotion is unaffected.
    """
    try:
        sub_scores = base_record.get("sub_scores") or {}
        if not isinstance(sub_scores, dict):
            sub_scores = {}
        # Embed CI resolution provenance in sub_scores.
        ci_resolution = base_record.get("ci_resolution")
        if isinstance(ci_resolution, dict):
            sub_scores = dict(sub_scores)  # make a copy to avoid mutating base_record
            sub_scores["ci_resolution"] = {
                "state": ci_resolution.get("state"),
                "resolved_sha": ci_resolution.get("resolved_sha"),
                "detail": ci_resolution.get("detail"),
            }

        confidence = base_record.get("confidence")
        threshold = base_record.get("threshold")
        verdict = QualificationVerdict(
            project_id=version.project_id,
            release_version_id=version.id,
            candidate_sha=base_record.get("candidate_sha"),
            confidence=(
                float(confidence)
                if isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                else 0.0
            ),
            sub_scores=sub_scores if sub_scores else None,
            blocker=base_record.get("blocker"),
            audit_missing_reason=base_record.get("audit_missing_reason") or None,
            would_promote=bool(would_promote),
            promotion_enabled=bool(promotion_enabled),
            terminal_status=terminal_status,
            threshold=(
                float(threshold)
                if isinstance(threshold, (int, float))
                and not isinstance(threshold, bool)
                else None
            ),
            started_at=_parse_iso_dt(base_record.get("started_at")),
            finalized_at=_parse_iso_dt(base_record.get("finalized_at")),
        )
        # Flush the caller's staged mutations into the outer transaction first,
        # so the savepoint below isolates ONLY the verdict INSERT.
        await session.flush()
        async with session.begin_nested():
            session.add(verdict)
            await session.flush()
    except Exception as exc:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.warning(
            "[RELEASE_QUALIFIER] failed to record qualification verdict for v%s "
            "(non-fatal — additive history only): %s",
            getattr(version, "version_number", "?"),
            exc,
        )


# ---------------------------------------------------------------------------
# promote — open + merge the automain → main PR.
# ---------------------------------------------------------------------------


async def _candidate_already_on_main(
    token: str,
    owner: str,
    repo: str,
    candidate_sha: str,
    version: release_version.ReleaseVersion,
    *,
    project: Project | None = None,
) -> bool:
    """Return True iff ``candidate_sha`` is already reachable from ``main``.

    Audit R4-C1 / R4-H1 — the single idempotency primitive for ``promote()``.
    A promotion is a multi-step GitHub operation (create release branch, open
    PR, merge).  If the process crashes between the merge LANDING on ``main``
    and the DB commit that records ``status="promoted"``, the next finalize /
    sweep re-enters ``promote()`` with ``last_qualification`` still ``pending``.
    Without this check the retry would (a) re-merge a closed/merged PR, or
    (b) hit a 405 "already merged" and misclassify a SUCCESSFUL promotion as a
    failure — leaving the DB and GitHub permanently divergent.

    This wraps ``github.is_ancestor_of(candidate_sha, promotion_target)``.  It
    answers the question covering BOTH retry sub-cases uniformly: "has this
    exact candidate commit already landed on the promotion target?".

    Returns ``False`` (NOT True) when the ancestry check could not be made
    (``is_ancestor_of`` returned ``None`` — API error, ref missing): a
    transient GitHub failure must never be read as "already promoted", which
    would skip a real merge.  A genuinely-already-merged candidate will be
    re-detected on the next retry once GitHub is reachable again.
    """
    _integration_branch, promotion_target = _lts_branch_names(project)
    try:
        ancestry = await github.is_ancestor_of(
            token,
            owner,
            repo,
            candidate_sha,
            promotion_target,
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a check failure must not abort promotion
        logger.warning(
            "[RELEASE_QUALIFIER] promote: idempotency ancestry check raised "
            "for v%d candidate %s (%s) — treating as not-yet-merged",
            version.version_number,
            (candidate_sha or "")[:8],
            exc,
        )
        return False
    if ancestry is None:
        # Indeterminate — degrade to "not merged" so a real merge still runs.
        logger.warning(
            "[RELEASE_QUALIFIER] promote: idempotency ancestry check for v%d "
            "candidate %s was indeterminate — treating as not-yet-merged",
            version.version_number,
            (candidate_sha or "")[:8],
        )
        return False
    return ancestry


async def _promotion_pr_landed(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    candidate_sha: str,
    version: release_version.ReleaseVersion,
    *,
    project: Project | None = None,
) -> bool:
    """Return True iff promotion PR ``pr_number`` has in fact already merged.

    Concern 3 — ``github.merge_pull_request`` returns ``{"merged": False}`` for
    BOTH a genuine merge block (failing checks / required reviews / conflict)
    AND an already-merged PR (GitHub's merge endpoint returns 405 once a PR has
    already been merged).  ``promote()`` must NOT record an already-merged PR as
    a ``merge_blocked`` failure — that permanently diverges the DB from GitHub.

    This disambiguates with TWO signals, strongest first:

      1. **The PR's own ``merged`` boolean** (``get_pull_request_merge_state``).
         This is a property of the PR resource itself; GitHub flips it to
         ``true`` synchronously the moment a merge completes.  It is therefore
         the AUTHORITATIVE answer and — crucially — is NOT subject to the
         eventual-consistency lag the ``GET /compare`` ancestry API can exhibit
         immediately after a merge.  A genuinely-successful promotion that the
         ancestry API would still report as ``diverged`` for a few seconds is
         correctly seen as merged here, so a real success is never misrecorded
         as ``merge_blocked``.

      2. **Ancestry fallback** (``_candidate_already_on_main``).  Used ONLY when
         the PR-state query is indeterminate (API error, PR not found).  Better
         than nothing: a crash-retry against a PR that merged-then-was-GC'd may
         no longer be queryable, but the candidate is still on ``main``.

    Returns ``True`` only on a positive merge signal from either tier; ``False``
    when both are negative or indeterminate — so a genuine merge block (the PR
    is open + unmerged, the candidate is not on main) is still correctly
    reported as not-landed and recorded as the failure it is.
    """
    try:
        pr_merged = await github.get_pull_request_merge_state(
            token, owner, repo, pr_number, project_id=str(version.project_id)
        )
    except Exception as exc:  # noqa: BLE001 — a state check must not abort promotion
        logger.warning(
            "[RELEASE_QUALIFIER] promote: PR-state merge check raised for "
            "PR #%s (v%d) (%s) — falling back to the ancestry check",
            pr_number,
            version.version_number,
            exc,
        )
        pr_merged = None

    if pr_merged is True:
        logger.info(
            "[RELEASE_QUALIFIER] promote: PR #%s for v%d reports merged=True on "
            "its own resource — the promotion landed",
            pr_number,
            version.version_number,
        )
        return True
    if pr_merged is False:
        # The PR resource itself says it is NOT merged — this is authoritative.
        # A merged=False merge result on a definitively-unmerged PR is a real
        # merge block; do not consult the lagging ancestry API to second-guess
        # it.
        return False

    # pr_merged is None — the PR state could not be determined.  Fall back to
    # the ancestry check (eventually-consistent, but the best remaining signal).
    logger.warning(
        "[RELEASE_QUALIFIER] promote: PR #%s for v%d merge state "
        "indeterminate — falling back to the candidate-on-main ancestry check",
        pr_number,
        version.version_number,
    )
    if not candidate_sha:
        return False
    return await _candidate_already_on_main(
        token, owner, repo, candidate_sha, version, project=project
    )


def _idempotent_pr_url(base_record: dict[str, Any] | None) -> str:
    """Resolve the ``pr_url`` to record on an idempotent-success path.

    Concern 5 — the idempotency success paths (candidate already on ``main``;
    PR-creation failed on a retry because the prior attempt's PR already merged)
    have no live PR object of their own to read an ``html_url`` from.  Recording
    a bare ``""`` is misleading — it reads like "there was no PR".

    When the caller threaded the original promotion attempt's PR URL through
    ``base_record["pr_url"]`` (a prior ``promotion_failed`` record on the same
    version carries it), reuse that real URL.  Otherwise return an explicit
    sentinel string so the recorded value is unambiguous rather than blank.
    """
    carried = (base_record or {}).get("pr_url")
    if isinstance(carried, str) and carried:
        return carried
    return "(already merged — no PR opened by this attempt)"


async def _relock_version_for_promotion_write(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    lock_released: bool,
) -> release_version.ReleaseVersion | None:
    """Resolve the version row, holding ``SELECT ... FOR UPDATE``, for the final write.

    Audit R4-H3 — ``promote()`` releases the ``ReleaseVersion`` row lock (via an
    intermediate ``session.commit()``) before its 30-120s of GitHub I/O so a
    concurrent ``run_qualification`` / reaper sweep / strategic completion is
    not stalled behind network calls.  Every terminal state write in
    ``promote()`` therefore goes through this helper.

    Returns the version instance with the row ``FOR UPDATE``-locked, or ``None``
    if the row vanished (project deleted mid-promotion) — the caller treats
    ``None`` as "nothing to write".

    ``lock_released`` makes the two cases EXPLICIT — this is the fix for the
    early-return ``expire()`` defect (concern 4):

      * ``lock_released=True`` — ``promote()`` already issued its intermediate
        commit.  The row lock is gone and the in-memory instance may be stale,
        so the row must be RE-READ and RE-LOCKED.  ``session.expire`` BEFORE the
        ``populate_existing`` SELECT mirrors the canonical re-lock pattern
        (``ci_completion._relock_merge_task`` / ``rollup_management._relock_parent``):
        ``populate_existing=True`` alone has been observed not to reliably
        refresh an instance still in the identity map on async sessions.  An
        ``expire`` here is safe: a just-committed instance has no un-flushed
        mutations to lose (the commit flushed them).

      * ``lock_released=False`` — an early-return failure path BEFORE the
        intermediate commit.  In ``promote()`` only the ``project_not_found``
        return reaches here (it is detected before the commit).  The row is
        STILL locked by this very transaction, and the live instance is already
        the authoritative locked row.  Calling ``session.expire`` here would
        DISCARD any un-flushed in-memory mutation on the version — the bug this
        parameter removes.  So on this path the helper does NOT expire and does
        NOT re-SELECT; it just confirms the row still exists and returns the
        live, already-locked instance for the caller to mutate.
    """
    # Snapshot the PK up front.  In the lock_released=True branch the
    # ``session.expire(version)`` below expires EVERY attribute on the instance
    # (including ``id``); reading ``version.id`` after that point triggers a
    # sync lazy-load, which under the async session raises MissingGreenlet
    # ("greenlet_spawn has not been called").  The reaper-sweep finalize path
    # hit this on every cycle — a passing-but-merge-conflicted promotion could
    # never record its verdict, so the version stuck at status="pending" and the
    # sweep crash-looped.  Holding the id in a local avoids the reload entirely.
    version_id = version.id

    if not lock_released:
        # The row is still locked by this transaction; the live instance is
        # authoritative.  A bare existence check (no expire — would drop
        # pending mutations; no re-SELECT — the lock is already held).
        still_exists = (
            await session.execute(
                select(release_version.ReleaseVersion.id).where(
                    release_version.ReleaseVersion.id == version_id
                )
            )
        ).first()
        if still_exists is None:
            logger.warning(
                "[RELEASE_QUALIFIER] promote: version row %s vanished before "
                "the final state write — skipping",
                version_id,
            )
            return None
        return version

    # lock_released=True — re-read + re-lock from authoritative DB state.
    session.expire(version)
    relocked = (
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
    if relocked is None:
        logger.warning(
            "[RELEASE_QUALIFIER] promote: version row %s vanished before the "
            "final state write — skipping",
            version_id,
        )
    return relocked


async def promote(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str,
    *,
    composite: dict[str, Any] | None = None,
    base_record: dict[str, Any] | None = None,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Promote the qualified candidate commit to ``main``.

    Audit C4: the qualifier children score the exact ``candidate_sha`` snapshot
    (their payloads are pinned to it), and ``automain`` legitimately keeps
    advancing during the 10-30 min they run.  Promotion therefore targets the
    EXACT ``candidate_sha`` — NOT live ``automain``:

      1. Create a release branch ``release-v{version_number}`` at
         ``candidate_sha``.  ``github.create_branch`` is idempotent (a 422
         "already exists" returns the existing ref); if a stale branch from a
         prior attempt points elsewhere it is deleted and recreated so the PR
         always carries exactly the qualified commit.
      2. Open a PR ``release-v{version_number} → main``.
      3. Merge it with ``merge_method="merge"`` (preserves integration
         history; branch protection on ``main`` runs CI as the final gate).
      4. Best-effort delete the ``release-v{version_number}`` branch (audit
         R4-H2 — dangling release branches must not accumulate forever).

    Because the PR is pinned to ``candidate_sha`` there is NO staleness guard:
    ``automain`` moving on after qualification is expected and harmless — the
    release branch is frozen at the scored commit regardless.

    **Idempotency (audit R4-C1 / R4-H1).**  A promotion is multi-step; a crash
    or retry between the merge landing on ``main`` and the DB commit would
    otherwise re-merge a closed PR or misclassify an already-merged PR (405) as
    a failure — diverging the DB from GitHub permanently.  ``promote()``
    therefore runs ONE ancestry check (``_candidate_already_on_main`` →
    ``github.is_ancestor_of``) (a) at the very start — if ``candidate_sha`` is
    already reachable from ``main`` the promotion already landed, so route
    straight to success — and (b) again before recording any non-merged result
    as a failure, so an open-but-already-merged PR is recorded as the success
    it actually is.

    **Lock safety (audit R4-H3).**  ``promote()``'s GitHub I/O (branch create,
    optional delete+recreate, PR create, merge, SHA re-resolve, tag, branch
    delete) is 30-120s of network calls.  The caller
    (``maybe_finalize_qualification`` / the sweep) holds a ``FOR UPDATE`` lock
    on the ``ReleaseVersion`` row; holding it across that I/O stalls every
    concurrent qualifier path.  So ``promote()`` issues an intermediate
    ``session.commit()`` to RELEASE that lock before the heavy I/O, runs the
    I/O unlocked, then RE-acquires ``FOR UPDATE`` (via
    ``_relock_version_for_promotion_write``) only for the final state write —
    re-checking ``version.status == "open"`` under the re-lock.  The idempotency
    check PLUS that re-check together keep a crash-retry or a concurrent
    finalize from double-promoting: if a parallel run already promoted, the
    re-locked row reads ``status="promoted"`` and this call bails as
    already-landed instead of opening a duplicate vN+1.

    CRITICAL: ``github.merge_pull_request`` does NOT enforce CI and does not
    raise on a 405/409 — it returns ``{"merged": False}``.  This function
    checks ``result.get("merged")`` and treats a non-merged result as a FAILED
    promotion (after the idempotency re-check): it does NOT retry blindly.

    On success: ``version.status = "promoted"``, stamps ``promoted_at`` /
    ``promoted_sha``, opens the next version (``build`` phase) via
    ``release_version.open_version``.  The recorded ``promoted_sha`` and the
    ``v{N}`` tag point at the REAL merge commit — ``merge_method="merge"``
    creates a new commit on ``main`` of which ``candidate_sha`` is only a
    parent, so the new ``main`` HEAD is re-resolved after the merge and used
    for ``promoted_sha``.  ``candidate_sha`` stays recorded as what was scored.
    The empty-diff branch keeps ``candidate_sha`` (release branch == main).

    On failure / non-merge: records the outcome in ``version.last_qualification``,
    keeps the version open, and sets ``phase = "stabilize"``.

    ``token``, ``owner``, ``repo`` are optional; when omitted (the normal
    finalize path — concern 2: ``_finalize_qualification`` deliberately does NOT
    pre-resolve them, since credential resolution is GitHub I/O) they are
    resolved here AFTER the intermediate commit has released the row lock, so no
    token round-trip ever runs under the lock.

    Operates on the ``session`` it is given — which the finalize chain runs as a
    DEDICATED session, so the intermediate commit only ever flushes qualifier
    state.  ``promote()`` issues that one intermediate commit itself; the
    caller (``_finalize_in_dedicated_session``) commits the final state write.
    Returns a result dict.
    """
    base_record = dict(base_record or {})
    project = await session.get(Project, version.project_id)
    _integration_branch, promotion_target = _lts_branch_names(project)
    if project is None:
        logger.warning(
            "[RELEASE_QUALIFIER] promote: project %s not found",
            version.project_id,
        )
        # Record a promotion failure so the version reaches a terminal
        # stabilize state.  A bare error return here would leave
        # last_qualification stuck at status="pending", and every later
        # _finalize_qualification call would re-loop on it forever.
        # This runs BEFORE the intermediate commit, so the version row is still
        # locked by this transaction — write directly (lock_released=False).
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "project_not_found",
            lock_released=False,
        )

    # --- Single-branch repo guard (LTS model inactive) ----------------------
    # When the integration branch IS the promotion target there is no separate
    # release trunk to promote onto: fleet work — including this candidate —
    # already merges directly to that branch during qualification.  Opening a
    # release PR (release-vN → promotion_target) would target the very branch
    # work lands on, which is a confusing no-op at best and an erroneous
    # self-merge at worst.  Mirror the back-merge guard
    # (hotfix_backmerge.reconcile_main_into_automain, which skips as
    # "lts_inactive" when the branches coincide): treat the LTS model as
    # inactive and record an inert, already-landed success.  The candidate is
    # already live on the single branch, so the version advances exactly like
    # the empty-diff path WITHOUT any GitHub I/O.  The row is still FOR UPDATE-
    # locked here (before the intermediate commit below), so write directly with
    # lock_released=False — the same pattern the project_not_found return uses.
    if _integration_branch == promotion_target:
        logger.info(
            "[RELEASE_QUALIFIER] promote: v%d integration branch (%s) is the "
            "promotion target — single-branch repo, LTS model inactive; "
            "candidate %s already live, recording inert success without a "
            "release PR",
            version.version_number,
            promotion_target,
            (candidate_sha or "")[:8],
        )
        return await _record_promotion_success(
            session,
            version,
            candidate_sha,
            {**base_record, "lts_inactive": True},
            "(single-branch repo — integration branch is the promotion "
            "target; no release PR opened)",
            empty_diff=True,
            already_landed=True,
            lock_released=False,
        )

    release_branch = f"release-v{version.version_number}"

    # --- Release the ReleaseVersion row lock BEFORE any GitHub I/O ----------
    # Audit R4-H3 + concern 2: the caller-supplied dedicated session holds a
    # FOR UPDATE lock on this version row (taken by _finalize_in_dedicated_session).
    # Token resolution AND the idempotency ancestry check are both GitHub I/O —
    # they must NOT run under that row lock, or every concurrent qualifier path
    # stalls behind the network.  So the intermediate commit happens HERE,
    # before token resolution and before the idempotency check — the lock is
    # released first, then all I/O runs unlocked.  The final state write
    # re-acquires the lock via _relock_version_for_promotion_write.
    #
    # The dedicated session has ONLY qualifier state dirty (concern 1), so this
    # commit can never prematurely commit a caller's unrelated transaction.
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — a commit failure here is recoverable
        logger.warning(
            "[RELEASE_QUALIFIER] promote: failed to commit before releasing "
            "the row lock for v%d (%s) — recording failure",
            version.version_number,
            exc,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] promote: rollback after pre-I/O commit "
                "failure also failed for v%d",
                version.version_number,
            )
        # The rollback released the lock — the failure record must re-lock.
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "lock_release_failed",
            detail=str(exc)[:300],
            lock_released=True,
        )

    # The row lock is now released — all the I/O below runs unlocked.
    if not token or not owner or not repo:
        resolved = await _resolve_github_with_session(session, project)
        if resolved is None:
            return await _record_promotion_failure(
                session,
                version,
                base_record,
                "no_credentials",
                lock_released=True,
            )
        token, owner, repo = resolved

    # --- Idempotency pre-check (audit R4-C1 / R4-H1) ------------------------
    # If candidate_sha is ALREADY reachable from main, a prior promotion
    # attempt landed the merge but crashed before the DB commit.  Route
    # straight to the success path (treat as already-landed) — do NOT re-merge
    # and do NOT record a failure.  This is a read-only GitHub call and now
    # runs unlocked (the intermediate commit above released the row lock).
    if candidate_sha and await _candidate_already_on_main(
        token, owner, repo, candidate_sha, version, project=project
    ):
        logger.info(
            "[RELEASE_QUALIFIER] promote: candidate %s for v%d is already "
            "reachable from main — a prior promotion landed; recording success "
            "without re-merging (idempotent retry)",
            candidate_sha[:8],
            version.version_number,
        )
        # Best-effort: clean up the release branch a prior attempt may have
        # left behind (audit R4-H2).
        await _delete_release_branch_best_effort(
            token, owner, repo, release_branch, version
        )
        return await _record_promotion_success(
            session,
            version,
            candidate_sha,
            base_record,
            # Concern 5: this idempotent-success path has no PR object of its
            # own.  Carry forward a pr_url the caller threaded through
            # base_record (the original promotion attempt's PR) when present;
            # otherwise label it explicitly rather than recording a bare "".
            pr_url=_idempotent_pr_url(base_record),
            token=token,
            owner=owner,
            repo=repo,
            already_landed=True,
        )

    # 0. Create the release branch pinned to the qualified candidate commit
    #    (audit C4).  Deterministic name so a retried promotion is idempotent.
    #    create_branch returns the existing ref on a 422; if that existing ref
    #    points at a different SHA (a stale branch from a failed prior attempt)
    #    delete + recreate it so the PR always carries exactly candidate_sha.
    try:
        branch_ref = await github.create_branch(
            token, owner, repo, release_branch, candidate_sha
        )
        existing_sha = (branch_ref or {}).get("object", {}).get("sha")
        if existing_sha and existing_sha != candidate_sha:
            logger.warning(
                "[RELEASE_QUALIFIER] promote: release branch %s already exists "
                "at %s (stale) — recreating at candidate %s for v%d",
                release_branch,
                existing_sha[:8],
                candidate_sha[:8],
                version.version_number,
            )
            await github.delete_branch(token, owner, repo, release_branch)
            await github.create_branch(
                token, owner, repo, release_branch, candidate_sha
            )
    except Exception as exc:  # noqa: BLE001 — a branch-creation failure blocks, never raises
        logger.warning(
            "[RELEASE_QUALIFIER] promote: failed to create release branch %s "
            "at %s for v%d: %s",
            release_branch,
            (candidate_sha or "")[:8],
            version.version_number,
            exc,
        )
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "release_branch_failed",
            detail=str(exc)[:300],
            lock_released=True,
        )

    # 1. Open the release-branch → promotion-target PR.  The release branch is
    #    frozen at candidate_sha, so this PR promotes exactly the qualified commit.
    try:
        pr = await github.create_pull_request(
            token,
            owner,
            repo,
            title=(
                f"Release v{version.version_number}: promote {release_branch} → {promotion_target}"
            ),
            head=release_branch,
            base=promotion_target,
            body=(
                f"Automated promotion of release v{version.version_number}.\n\n"
                f"Candidate SHA: `{candidate_sha}`\n"
                f"Release branch: `{release_branch}` (pinned to the qualified "
                f"commit)\n"
                f"Composite confidence: "
                f"{(composite or {}).get('confidence', 'n/a')}\n\n"
                f"Opened by the release qualifier "
                f"(backend/app/release_qualifier.py)."
            ),
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a PR-creation failure blocks, never raises
        # Audit R4-H1: a PR-creation failure on a RETRY is the classic
        # divergence trap — the prior attempt's PR merged then closed, so
        # create_pull_request finds nothing open and raises.  Re-run the
        # idempotency check before recording a failure: if the candidate is
        # already on main, this is a successful prior promotion, not a failure.
        if candidate_sha and await _candidate_already_on_main(
            token, owner, repo, candidate_sha, version, project=project
        ):
            logger.info(
                "[RELEASE_QUALIFIER] promote: PR creation failed for v%d but "
                "candidate %s is already on main — a prior promotion landed; "
                "recording success (idempotent retry)",
                version.version_number,
                candidate_sha[:8],
            )
            await _delete_release_branch_best_effort(
                token, owner, repo, release_branch, version
            )
            return await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                # Concern 5: no live PR object here (creation failed); carry a
                # caller-threaded pr_url through if present, else label it.
                pr_url=_idempotent_pr_url(base_record),
                token=token,
                owner=owner,
                repo=repo,
                already_landed=True,
            )
        logger.warning(
            "[RELEASE_QUALIFIER] failed to open promotion PR for v%d: %s",
            version.version_number,
            exc,
        )
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "pr_creation_failed",
            detail=str(exc)[:300],
            lock_released=True,
        )

    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")

    # An empty diff (automain == main) is a degenerate but valid outcome:
    # create_pull_request returns {"empty_diff": True, "merged": True}.  Nothing
    # to promote — treat as a no-op success and still advance the version.
    if pr.get("empty_diff"):
        logger.info(
            "[RELEASE_QUALIFIER] v%d promotion: automain == main (empty diff)",
            version.version_number,
        )
        await _delete_release_branch_best_effort(
            token, owner, repo, release_branch, version
        )
        return await _record_promotion_success(
            session,
            version,
            candidate_sha,
            base_record,
            pr_url,
            token=token,
            owner=owner,
            repo=repo,
            empty_diff=True,
        )

    if not pr_number:
        # Concern 5: a PR with no number cannot be merged — but create_branch
        # already ran, so the release-v{N} branch exists.  Clean it up
        # best-effort before recording the failure so it does not dangle.
        await _delete_release_branch_best_effort(
            token, owner, repo, release_branch, version
        )
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "pr_missing_number",
            pr_url=pr_url,
            lock_released=True,
        )

    # --- Pre-merge Alembic dual-head guard (parity with the rollup, hotfix
    #     back-merge, and CI task-PR merge paths) -----------------------------
    # The promotion merge lands the release branch's added migrations onto the
    # promotion target. The qualification-time structural sync gate is
    # commit-ancestry based (not migration-aware) and deliberately bounded — it
    # falls through and promotes after a delay when the back-merge looks stuck,
    # leaving a window where a hotfix migration can land on the promotion target
    # AFTER qualification but BEFORE this merge. Because the merge API does not
    # enforce CI, there is no backstop at merge time. Validate the combined
    # migration chain against the promotion target's CURRENT chain immediately
    # before merging, reusing the SAME shared validator the other gates use.
    #
    # On a non-clean verdict — a real dual-head / duplicate-revision / stale-
    # anchor collision OR an inability to fetch/parse the migration metadata —
    # DO NOT call the merge API. Hold the promotion in a RETRYABLE state:
    # _record_promotion_failure keeps the version open and bounces it to
    # stabilize (main is untouched), so the next strategic cycle re-qualifies
    # once the main → automain back-merge reconciles the integration branch.
    # This is the missing hard backstop at merge time, NOT a replacement for the
    # structural sync gate (which stays as-is).
    from app.rollup_migration_validation import enforce_premerge_migration_guard

    ok_to_merge, hold_reason = await enforce_premerge_migration_guard(
        token, owner, repo, pr_number, promotion_target
    )
    if not ok_to_merge:
        logger.warning(
            "[RELEASE_QUALIFIER] promote: held merge of promotion PR #%s for "
            "v%d — Alembic combined-chain guard: %s",
            pr_number,
            version.version_number,
            hold_reason,
        )
        # The merge never ran; clean up the release branch best-effort so it
        # does not dangle while the version waits to re-qualify.
        await _delete_release_branch_best_effort(
            token, owner, repo, release_branch, version
        )
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "migration_guard_blocked",
            detail=hold_reason,
            pr_url=pr_url,
            lock_released=True,
        )

    # 2. Merge the PR — merge_method="merge" preserves integration history.
    #    merge_pull_request does NOT raise on 405/409; it returns merged=False.
    try:
        merge_result = await github.merge_pull_request(
            token,
            owner,
            repo,
            pr_number,
            merge_method="merge",
            project_id=str(version.project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a 5xx raises HTTPException; treat as failure
        logger.warning(
            "[RELEASE_QUALIFIER] merge raised for promotion PR #%s (v%d): %s",
            pr_number,
            version.version_number,
            exc,
        )
        return await _record_promotion_failure(
            session,
            version,
            base_record,
            "merge_error",
            detail=str(exc)[:300],
            pr_url=pr_url,
            lock_released=True,
        )

    if not merge_result.get("merged"):
        # Concern 3 — disambiguate a merged=False result.  merge_pull_request
        # collapses TWO different outcomes onto {"merged": False}: a genuine
        # merge block (failing checks / required reviews / conflict) AND an
        # already-merged PR (GitHub's merge endpoint returns 405 once a PR is
        # already merged).  Recording an already-merged PR as merge_blocked
        # would permanently diverge the DB from GitHub.
        #
        # _promotion_pr_landed asks the PR resource itself for its
        # authoritative ``merged`` boolean — which flips synchronously on a
        # merge and is NOT subject to the compare API's eventual-consistency
        # lag that could make a genuinely-successful promotion look like a
        # merge_blocked failure right after the merge.  It falls back to the
        # ancestry check only when the PR state is indeterminate.
        if await _promotion_pr_landed(
            token, owner, repo, pr_number, candidate_sha, version, project=project
        ):
            logger.info(
                "[RELEASE_QUALIFIER] promote: PR #%s for v%d returned "
                "merged=False but the PR is in fact merged — recording success "
                "(idempotent retry / eventual-consistency-safe)",
                pr_number,
                version.version_number,
            )
            await _delete_release_branch_best_effort(
                token, owner, repo, release_branch, version
            )
            merged_sha_retry: str | None = None
            try:
                merged_sha_retry = await github.get_branch_sha(
                    token, owner, repo, promotion_target
                )
            except Exception:  # noqa: BLE001 — best-effort; degrade to candidate
                logger.warning(
                    "[RELEASE_QUALIFIER] promote: could not re-resolve %s "
                    "HEAD after detecting an already-merged PR #%s for v%d — "
                    "recording candidate_sha",
                    promotion_target,
                    pr_number,
                    version.version_number,
                )
            return await _record_promotion_success(
                session,
                version,
                candidate_sha,
                base_record,
                pr_url,
                token=token,
                owner=owner,
                repo=repo,
                promoted_sha=merged_sha_retry or candidate_sha,
                already_landed=True,
            )

        # Distinguish a real merge conflict (GitHub 409 → conflict=True) from
        # pending required checks (GitHub 405 → conflict=False/None).  A real
        # conflict cannot self-heal and must fail immediately; a pending-CI
        # block WILL clear once checks pass, so arrange for GitHub to complete
        # the merge automatically rather than recording a spurious failure.
        if merge_result.get("conflict"):
            msg = merge_result.get("message", "merge not completed")
            logger.warning(
                "[RELEASE_QUALIFIER] promotion PR #%s for v%d did not merge "
                "(real conflict): %s",
                pr_number,
                version.version_number,
                msg,
            )
            return await _record_promotion_failure(
                session,
                version,
                base_record,
                "merge_blocked",
                detail=str(msg)[:300],
                pr_url=pr_url,
                conflict=True,
                lock_released=True,
            )

        # CI still pending (GitHub 405, no conflict) — arrange for GitHub to
        # complete the merge once required checks pass.  Don't record a failure
        # verdict; last_qualification stays "pending" so the reaper re-checks
        # and _candidate_already_on_main records idempotent success when the
        # auto-merge lands.
        msg = merge_result.get("message", "merge not completed")
        try:
            am_result = await github.enable_auto_merge(
                token,
                owner,
                repo,
                pr_number,
                merge_method="MERGE",
                project_id=str(version.project_id),
            )
        except Exception as exc:  # noqa: BLE001
            am_result = {"enabled": False, "message": str(exc)[:200]}
        auto_merge_enabled = bool(am_result.get("enabled"))
        logger.info(
            "[RELEASE_QUALIFIER] promote: PR #%s for v%d pending CI — "
            "auto-merge %s (%s). Qualification stays pending; reaper confirms "
            "when auto-merge lands.",
            pr_number,
            version.version_number,
            "enabled" if auto_merge_enabled else "not confirmed",
            am_result.get("message", ""),
        )
        # Durably record the promotion sub-state so the reaper's qualification-
        # timeout backstop can distinguish this version (qualification PASSED,
        # waiting on the promotion PR's CI) from a version whose qualifier
        # children never terminated.  Re-lock the version row first because the
        # intermediate commit above released the lock.
        locked = await _relock_version_for_promotion_write(
            session, version, lock_released=True
        )
        if locked is not None and locked.status == "open":
            q = dict(locked.last_qualification or {})
            q["promotion_pending"] = {
                "pr_number": pr_number,
                "pr_url": pr_url,
                "auto_merge_enabled": auto_merge_enabled,
                "enabled_at": _utcnow().isoformat(),
            }
            locked.last_qualification = q
            session.add(locked)
        return {
            "status": "pending_auto_merge",
            "pr_number": pr_number,
            "pr_url": pr_url,
            "auto_merge_enabled": auto_merge_enabled,
        }

    # 3. Success — resolve the REAL merged commit.  merge_method="merge"
    #    creates a NEW merge commit on main; candidate_sha is only one of its
    #    parents, not main HEAD.  github.merge_pull_request does not return the
    #    merge SHA, so re-resolve main HEAD — that commit IS the merge result.
    #    A resolution failure here is non-fatal: the merge already landed, so
    #    degrade to candidate_sha (a known parent) rather than fail a
    #    successful promotion.
    merged_sha: str | None = None
    try:
        merged_sha = await github.get_branch_sha(token, owner, repo, promotion_target)
    except Exception as exc:  # noqa: BLE001 — merge already succeeded; SHA read is best-effort
        logger.warning(
            "[RELEASE_QUALIFIER] promote: merged PR #%s for v%d but could not "
            "resolve the new %s HEAD (%s) — recording candidate_sha instead",
            pr_number,
            version.version_number,
            promotion_target,
            exc,
        )

    # 4. Best-effort: delete the release branch now that the merge has landed
    #    (audit R4-H2 — release-v{N} branches must not accumulate forever).
    await _delete_release_branch_best_effort(
        token, owner, repo, release_branch, version
    )

    logger.info(
        "[RELEASE_QUALIFIER] v%d promoted: automain → main merged via PR #%s "
        "(merge commit=%s)",
        version.version_number,
        pr_number,
        (merged_sha or candidate_sha)[:8],
    )
    return await _record_promotion_success(
        session,
        version,
        candidate_sha,
        base_record,
        pr_url,
        token=token,
        owner=owner,
        repo=repo,
        promoted_sha=merged_sha or candidate_sha,
    )


async def _delete_release_branch_best_effort(
    token: str,
    owner: str,
    repo: str,
    release_branch: str,
    version: release_version.ReleaseVersion,
) -> None:
    """Delete the ``release-v{N}`` branch after a promotion — best-effort.

    Audit R4-H2: the release branch is a throwaway pointer at the qualified
    commit; once the promotion PR has merged (or the candidate is confirmed on
    main) it serves no purpose.  Left behind, ``release-v1``, ``release-v2``, …
    accumulate on the repo forever.

    A delete failure must NEVER fail or abort the promotion — the merge has
    already landed; a dangling branch is cosmetic clutter, not a correctness
    problem.  Same best-effort contract as the ``v{N}`` tag creation: wrapped
    in try/except, logs a warning only.
    """
    try:
        deleted = await github.delete_branch(token, owner, repo, release_branch)
        if deleted:
            logger.info(
                "[RELEASE_QUALIFIER] promote: deleted release branch %s for v%d",
                release_branch,
                version.version_number,
            )
        else:
            # delete_branch returns False (not raises) on a non-204 — e.g. the
            # branch is already gone.  Not an error; just note it.
            logger.info(
                "[RELEASE_QUALIFIER] promote: release branch %s for v%d not "
                "deleted (already gone or non-204) — non-fatal",
                release_branch,
                version.version_number,
            )
    except Exception as exc:  # noqa: BLE001 — branch delete failure must not abort promotion
        logger.warning(
            "[RELEASE_QUALIFIER] promote: failed to delete release branch %s "
            "for v%d (non-fatal): %s",
            release_branch,
            version.version_number,
            exc,
        )


async def _record_promotion_failure(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    base_record: dict[str, Any],
    error: str,
    *,
    detail: str | None = None,
    pr_url: str | None = None,
    conflict: bool = False,
    lock_released: bool,
) -> dict[str, Any]:
    """Record a failed promotion: keep the version open, set phase=stabilize.

    A failed promotion only ever *blocks* — the version stays open and goes
    back to stabilize so the next strategic cycle works the blocker.  ``main``
    is untouched, so this is always safe.

    Audit R4-H3: resolves the ``ReleaseVersion`` row (``FOR UPDATE``-locked) via
    ``_relock_version_for_promotion_write`` before writing.  ``lock_released``
    tells that helper whether ``promote()`` has already issued its intermediate
    commit: ``True`` ⇒ re-read + re-lock from the DB; ``False`` ⇒ an early-return
    path before the commit, the row is still locked by this transaction so the
    live instance is used directly without an ``expire()`` that would drop
    pending mutations (concern 4).

    Round 9 (data-integrity): re-checks ``version.status == "open"`` under the
    re-lock — the SAME guard ``_record_promotion_success`` already has.  Two
    finalize/promote calls can race (the completion-handler finalize and the
    reaper-sweep finalize run in separate concurrent transactions); if one
    promotes successfully while the loser fails transiently, the loser must NOT
    overwrite ``last_qualification`` with ``{status: "promotion_failed", ...}``
    on a row that already reads ``status="promoted"`` — that would leave a
    self-contradictory terminal record.  When the re-locked row is no longer
    ``open`` this returns an ``already_promoted``-style result WITHOUT touching
    ``last_qualification``, exactly mirroring ``_record_promotion_success``.
    """
    locked = await _relock_version_for_promotion_write(
        session, version, lock_released=lock_released
    )
    if locked is None:
        # Row vanished (project deleted mid-promotion) — nothing to record.
        return {"status": "promotion_failed", "error": error, "conflict": conflict}
    version = locked
    # Re-check under the re-lock (Round 9 — data integrity): a concurrent
    # finalize / crash-retry may have already PROMOTED this version while this
    # call's GitHub I/O ran unlocked.  An already-promoted (or abandoned)
    # version is terminal — overwriting last_qualification with a
    # promotion_failed record here would contradict version.status="promoted".
    # Bail to a result that reflects the already-recorded state, leaving
    # last_qualification untouched.  Mirrors _record_promotion_success.
    if version.status != "open":
        logger.info(
            "[RELEASE_QUALIFIER] promote: v%d is already status=%s under the "
            "re-lock when recording a '%s' failure — a concurrent "
            "finalize/retry already promoted it; not overwriting "
            "last_qualification (idempotent)",
            version.version_number,
            version.status,
            error,
        )
        existing = version.last_qualification or {}
        return {
            "status": existing.get("status", version.status),
            "promoted_sha": existing.get("promoted_sha") or version.promoted_sha,
            "next_version_number": None,
            "already_promoted": True,
        }
    record = {
        **base_record,
        "status": "promotion_failed",
        "error": error,
    }
    if detail:
        record["detail"] = detail
    if pr_url:
        record["pr_url"] = pr_url
    if conflict:
        record["conflict"] = True
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    # Defensive (audit R4 MEDIUM): only flip phase on a still-open version.  An
    # already-promoted/abandoned version is terminal — set_version_phase would
    # be a meaningless write (and the version should never reach here promoted).
    if version.status == "open":
        release_version.set_version_phase(
            version,
            "stabilize",
            reasoning=(
                f"promotion v{version.version_number} failed ({error}); "
                f"returning to stabilize"
            ),
        )
    session.add(version)
    # Phase 4: durable verdict-history row.  A promotion_failed candidate DID
    # clear the qualifier (would_promote=True) — the failure is in the merge
    # infrastructure, not the score — and promotion only ever runs when enabled.
    await _record_qualification_verdict(
        session,
        version,
        record,
        terminal_status="promotion_failed",
        would_promote=True,
        promotion_enabled=True,
    )
    logger.warning(
        "[RELEASE_QUALIFIER] promotion of v%d failed (%s) — version stays open, "
        "phase=stabilize",
        version.version_number,
        error,
    )
    failure_detail = f": {detail}" if detail else ""
    await _notify_operator(
        session,
        version,
        "release_promotion_failed",
        (
            f"Promotion of v{version.version_number} failed — it cleared the "
            f"qualifier but the automain → main merge failed "
            f"({error}{failure_detail}). main was NOT advanced; the version "
            f"stays open and returns to stabilize."
        ),
        extra={
            "error": error,
            "detail": detail,
            "pr_url": pr_url,
            "conflict": conflict,
        },
    )
    return {"status": "promotion_failed", "error": error, "conflict": conflict}


async def _record_promotion_success(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str,
    base_record: dict[str, Any],
    pr_url: str,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    empty_diff: bool = False,
    promoted_sha: str | None = None,
    already_landed: bool = False,
    lock_released: bool = True,
) -> dict[str, Any]:
    """Record a successful promotion and open the next version.

    Marks ``version`` ``status="promoted"``, stamps ``promoted_at`` /
    ``promoted_sha``, then opens version N+1 in the ``build`` phase with its
    ``base_sha`` set to the just-promoted SHA.

    ``promoted_sha`` is the commit that actually became ``main`` HEAD.  For a
    real merge that is the NEW merge commit ``merge_method="merge"`` produced
    (``candidate_sha`` — automain HEAD — is only a *parent* of it), so
    ``promote()`` re-resolves ``main`` HEAD after the merge and passes it here.
    When omitted it defaults to ``candidate_sha`` — correct for the empty-diff
    branch, where ``automain == main`` and nothing new was committed.  The
    version row, the next version's ``base_sha`` and the ``v{N}`` tag all use
    this ``promoted_sha``.

    Audit R4-H3 / R4-C1: resolves the ``ReleaseVersion`` row (``FOR UPDATE``-
    locked) via ``_relock_version_for_promotion_write`` and RE-CHECKS
    ``version.status == "open"`` under that lock.  If the row already reads
    ``status="promoted"`` a concurrent finalize / a crash-retry already promoted
    this version — bail as already-landed WITHOUT opening a duplicate vN+1,
    instead of double-promoting.  Every ``promote()`` success path reaches here
    AFTER the intermediate commit, so ``lock_released`` defaults to ``True``
    (re-read + re-lock from the DB).  ``already_landed`` is True when
    ``promote()`` reached here via an idempotency path (the merge was already on
    ``main``); it only affects logging.

    After the version is marked promoted, creates a git tag
    ``v{version.version_number}`` on the promoted SHA.  A tag-creation failure
    is non-fatal — it is wrapped in try/except and only logs a warning.  The
    merge already succeeded; the tag is cosmetic convenience.
    """
    _record_project = await session.get(Project, version.project_id)
    integration_branch, promotion_target = _lts_branch_names(_record_project)
    locked = await _relock_version_for_promotion_write(
        session, version, lock_released=lock_released
    )
    if locked is None:
        # Row vanished — the merge landed on GitHub but the version row is
        # gone (project deleted mid-promotion).  Nothing left to write.
        return {
            "status": "promoted",
            "promoted_sha": promoted_sha or candidate_sha,
            "next_version_number": None,
        }
    version = locked

    # Re-check under the re-lock (audit R4-C1 / R4-H3): a concurrent finalize
    # or a crash-retry may have already promoted this version while promote()'s
    # GitHub I/O ran unlocked.  An already-promoted (or abandoned) version is
    # terminal — re-promoting would open a duplicate vN+1.  Bail to a success
    # return that reflects the already-recorded state.
    if version.status != "open":
        logger.info(
            "[RELEASE_QUALIFIER] promote: v%d is already status=%s under the "
            "re-lock — a concurrent finalize/retry already promoted it; not "
            "re-promoting (idempotent)",
            version.version_number,
            version.status,
        )
        existing = version.last_qualification or {}
        return {
            "status": "promoted",
            "promoted_sha": existing.get("promoted_sha")
            or version.promoted_sha
            or promoted_sha
            or candidate_sha,
            "next_version_number": None,
            "already_promoted": True,
        }

    # The commit that is now main HEAD.  Falls back to candidate_sha for the
    # empty-diff path (automain == main, so they are the same commit).
    effective_sha = promoted_sha or candidate_sha
    now = _utcnow()
    record = {
        **base_record,
        "status": "promoted",
        "candidate_sha": candidate_sha,
        "promoted_sha": effective_sha,
        "pr_url": pr_url,
    }
    if empty_diff:
        record["empty_diff"] = True
    if already_landed:
        record["already_landed"] = True
    version.last_qualification = record
    flag_modified(version, "last_qualification")
    version.status = "promoted"
    version.promoted_at = now
    version.promoted_sha = effective_sha
    session.add(version)
    # Phase 4: durable verdict-history row for a successful promotion.  This
    # only ever runs with promotion enabled (the dry_run branch returns earlier).
    await _record_qualification_verdict(
        session,
        version,
        record,
        terminal_status="promoted",
        would_promote=True,
        promotion_enabled=True,
    )

    # Open the next version (build phase) — base_sha is the freshly-promoted SHA
    # (the real main HEAD after the merge, not the now-superseded candidate).
    # goal snapshots the project's current focus (see release_version.open_version).
    _next_goal: str | None = None
    try:
        from app.problem_model import parse_problem_model

        if _record_project is not None:
            _next_goal = (
                parse_problem_model(_record_project.problem_model).intent.current_focus
                or None
            )
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] could not read current_focus for next version goal"
        )
    next_version = await release_version.open_version(
        session,
        version.project_id,
        version_number=version.version_number + 1,
        base_sha=effective_sha,
        goal=_next_goal,
    )
    logger.info(
        "[RELEASE_QUALIFIER] v%d promoted (sha=%s%s); opened v%d",
        version.version_number,
        effective_sha[:8],
        " — already-landed retry" if already_landed else "",
        next_version.version_number,
    )

    # Best-effort: create a git tag v{version_number} on the promoted SHA.
    # A tag-creation failure must NEVER abort or fail the promotion — the merge
    # already succeeded and the tag is cosmetic convenience only.
    if token and owner and repo:
        tag_name = f"v{version.version_number}"
        try:
            await github.create_tag(
                token,
                owner,
                repo,
                tag_name,
                effective_sha,
                message=f"Release {tag_name} ({integration_branch} → {promotion_target} promotion)",
                project_id=str(version.project_id),
            )
            logger.info(
                "[RELEASE_QUALIFIER] created release tag %s on %s",
                tag_name,
                effective_sha[:8],
            )
        except Exception as exc:  # noqa: BLE001 — tag failure must not abort promotion
            logger.warning(
                "[RELEASE_QUALIFIER] failed to create release tag %s (non-fatal): %s",
                tag_name,
                exc,
            )

    await _notify_operator(
        session,
        version,
        "release_promoted",
        (
            f"Released v{version.version_number} — {integration_branch} → {promotion_target} promoted "
            f"(confidence {float(base_record.get('confidence') or 0.0):.2f}). "
            f"Production deploy is running; v{next_version.version_number} is "
            f"now open for build."
        ),
        extra={
            "confidence": base_record.get("confidence"),
            "sub_scores": base_record.get("sub_scores"),
            "promoted_sha": effective_sha,
            "pr_url": pr_url,
            "next_version_number": next_version.version_number,
        },
    )
    return {
        "status": "promoted",
        "promoted_sha": effective_sha,
        "next_version_number": next_version.version_number,
    }
