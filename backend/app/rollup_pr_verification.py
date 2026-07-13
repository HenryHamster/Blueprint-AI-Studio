"""Rollup PR-merge verification seam.

Extracted from ``rollup_management.py`` to keep that module under its size
guardrail. Contains the PR-merge status lookup (``_verify_pr_merged``) plus its
negative cache, the review-respawn cap constant/counter, and the rollup-side
terminal-failure bookkeeping it depends on.

These symbols are re-exported from ``app.rollup_management`` so existing caller
and test import paths keep working unchanged.

All deferred imports from ``app.main`` / routers are intentional — they avoid
circular imports and preserve test-patchability.
"""

# fmt: off
import logging
import time
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task,
    Artifact,
    TaskStatus,
    ArtifactType,
    Project,
)
from app.branch_resolution import merged_pr_counts_as_integration_landing
from app.landed_signal import (
    mark_landed_on_integration,
    mark_stranded_non_landing,
    mark_unblocked_non_merge,
)

logger = logging.getLogger(__name__)


async def verify_rollup_merge_landed(
    task: Task,
    project: "Project | None",
    session: AsyncSession,
    *,
    pr_number: int | None,
    merge_commit_sha: str | None = None,
    integration_branch: str | None = None,
    source: str,
) -> str:
    """Tri-state ancestry gate run BEFORE stamping a rollup integration landing.

    Confirms the rollup PR's merge commit is actually an ancestor of the
    project's integration branch before a caller stamps ``pr_merged`` / the
    shipped badge on the rollup-orig (``ci_completion``) or the rollup parent
    (``rollup_management``). Both stamping sites historically trusted a pure
    branch-NAME equality (``base == integration_branch``) which can be true while
    the merge commit never actually reached the integration trunk (e.g. a rollup
    chain whose ``rollup -> automain`` step never completed), inflating merge-rate
    and shipping false badges.

    Mirrors the precedent in :mod:`app.research_completion` (the non-terminal
    parent ancestry block): a DEFINITIVE negative strands the task; anything
    indeterminate preserves flag-trust so a transient GitHub outage never blocks
    completion. ``None`` is never coerced to ``False``.

    Returns one of:
      * ``"landed"``        — ``is_ancestor_of`` returned ``True``. The merge
        commit is on the integration branch; the caller stamps as today.
      * ``"indeterminate"`` — ``is_ancestor_of`` returned ``None``, no merge
        commit SHA was available, or GitHub credentials / the PR / the compare
        call could not be resolved. The caller PRESERVES current flag-trust
        stamping.
      * ``"stranded"``      — ``is_ancestor_of`` returned a definitive ``False``.
        This helper has already recorded the durable stranded marker via
        :func:`mark_stranded_non_landing` and raised the operator alert via
        ``_maybe_create_failure_alert``; the caller must NOT stamp ``pr_merged``.

    Reuses the credential-resolution / PR-fetch pattern of
    ``research_completion`` and the existing ``app.github`` helpers; never raises
    for a GitHub/credential error (it degrades to ``"indeterminate"``).
    """
    from app import github as _gh
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.branch_resolution import get_project_integration_branch

    if project is None:
        return "indeterminate"

    project_id = str(getattr(task, "project_id", "") or "") or None

    # Resolve GitHub credentials via the project (same order as
    # research_completion's ancestry block). A credential failure -> indeterminate
    # so a transient outage never blocks rollup completion.
    creds: tuple | None = None
    try:
        from app.release_qualifier import _resolve_github_with_session

        creds = await _resolve_github_with_session(session, project)
    except Exception as exc:
        logger.debug(
            "[ROLLUP_ANCESTRY] credential resolution skipped for %s: %s", source, exc
        )
        return "indeterminate"
    if creds is None:
        return "indeterminate"
    token, owner, repo = creds

    # Resolve the merge commit SHA from the merged PR when the caller did not
    # supply one (``merge_pull_request`` does not return it). No merge commit
    # SHA -> indeterminate (the PR may not be merged yet, or the API omitted it).
    if not merge_commit_sha and pr_number is not None:
        try:
            pr_data = await _gh.get_pull_request(
                token, owner, repo, pr_number, project_id=project_id
            )
            merge_commit_sha = (pr_data or {}).get("merge_commit_sha")
        except Exception as exc:
            logger.warning(
                "[ROLLUP_ANCESTRY] PR #%s fetch failed (transient) for %s: %s — degrading to indeterminate",
                pr_number,
                source,
                exc,
            )
            return "indeterminate"
    if not merge_commit_sha:
        return "indeterminate"

    if integration_branch is None:
        integration_branch = get_project_integration_branch(project)

    try:
        is_anc = await _gh.is_ancestor_of(
            token,
            owner,
            repo,
            merge_commit_sha,
            integration_branch,
            project_id=project_id,
        )
    except Exception as exc:
        logger.warning(
            "[ROLLUP_ANCESTRY] is_ancestor_of raised for %s: %s — degrading to indeterminate",
            source,
            exc,
        )
        return "indeterminate"

    if is_anc is True:
        return "landed"
    if is_anc is None:
        # Compare API error / ref not found. None is never coerced to False.
        return "indeterminate"

    # DEFINITIVE negative: the merge commit is NOT reachable from integration.
    reason = (
        f"Rollup merge commit {merge_commit_sha[:8]} (PR #{pr_number}) is NOT an "
        f"ancestor of integration branch {integration_branch!r}; the "
        f"rollup->integration merge did not land. Leaving pr_merged/shipped unset "
        f"and marking the task stranded ({source})."
    )
    _had_pr_merged = bool((task.context or {}).get("pr_merged"))
    mark_stranded_non_landing(task, reason=reason)
    logger.error("[ROLLUP_ANCESTRY] %s task=%s", reason, getattr(task, "id", None))
    from app.direction_outcomes import discount_stranded_pr_from_direction_credit
    await discount_stranded_pr_from_direction_credit(
        task, session, had_pr_merged=_had_pr_merged
    )
    try:
        await _maybe_create_failure_alert(task, session, reason)
    except Exception as exc:
        logger.warning(
            "[ROLLUP_ANCESTRY] stranded alert failed for task %s: %s",
            getattr(task, "id", None),
            exc,
        )
    return "stranded"


# Circuit-breaker: cap how many times the stale-PR rollup recovery in
# ``_check_and_trigger_rollup`` respawns a review for one unmerged child.
# Without a cap, a review that keeps failing fast — e.g. pre-empted at dispatch
# — is respawned every rollup cycle unbounded (2026-05-09 incident: 795
# SCOPE_EMPTY review tasks in 9.5h). Past the cap the rollup escalates the
# parent to manual approval instead of looping.
MAX_REVIEW_RESPAWNS = 5


def _review_respawn_count(task: Task) -> int:
    """Times the stale-PR rollup recovery has respawned a review for *task*.

    Stored in ``context._review_respawn_count``; capped by ``MAX_REVIEW_RESPAWNS``.
    The key is underscore-prefixed so ``_spawn_pipeline_task``'s context
    propagation (which copies all non-underscore keys onto the spawned review
    task) drops it — a non-prefixed key would leak a stale count onto every
    review task spawned for this PR.
    Tolerates a missing/None context or a non-numeric value (-> 0).
    """
    try:
        return int((task.context or {}).get("_review_respawn_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


# Module-level negative cache for PR-merge verification keyed by task.id.
# When a cycle discovers a child's PR isn't merged yet, the reaper's next
# 60s tick (plus any fan-out from sibling completion events) would otherwise
# re-fetch the same PR.  Stamping here is session-independent — unlike a
# task.context mutation, which only persists if the caller happens to commit
# the attached object.  A 30s TTL is short enough that a merge happening
# between checks is detected within one cycle.
_PR_MERGE_NEGATIVE_TTL_S = 30
_pr_merge_negative_cache: dict[uuid.UUID, float] = {}


class _TransientPRVerifyError(Exception):
    """Internal marker: this PR could not be verified because GitHub failed
    transiently (5xx / network error / a GitHub RATE LIMIT, which
    ``github_pulls.get_pull_request`` surfaces as a **503 + Retry-After** via the
    canonical ``github_http.raise_rate_limited``; 503 is already in the retryable
    status list below, so a drained quota bucket takes this clean transient path
    rather than escaping as an unclassified raise).

    Raised instead of returning early so the transient path FALLS THROUGH to the
    negative-cache stamp at the bottom of :func:`_verify_pr_merged`. The early
    returns it replaces bypassed that stamp, which meant the ONLY throttle on the
    per-candidate, per-cycle verification (`_check_candidate_task_deps` calls it
    for every same-base COMPLETED ``create`` dep of every candidate, on every ~5s
    dispatch cycle) disappeared exactly when GitHub was already unhealthy — and
    each un-cached verify is up to 3 HTTP calls, since ``_github_request``
    retries 5xx. A partial GitHub outage was thereby converted into full
    shared-token-bucket exhaustion. The 30s TTL still bounds the staleness: a
    merge is detected within one cycle of the window elapsing.
    """


def _record_rollup_failure_signals(task: Task, failure_class: str) -> None:
    """Mirror agent_status's terminal-failure bookkeeping for rollup-side FAILEDs.

    Without this, parent failures terminated inside _check_and_trigger_rollup
    never reach deploy_monitoring['task_results'] or the per-direction streak,
    so anomaly detection and direction-hold logic can't see them.
    """
    from app.routers.agent_status import record_task_result
    from app.shared_state import record_direction_result

    record_task_result(
        success=False,
        failure_class=failure_class,
        task_id=str(task.id),
        task_type=task.first_payload.get("type", ""),
    )
    direction_id = str((task.context or {}).get("direction_id") or "")
    if direction_id:
        record_direction_result(
            direction_id,
            success=False,
            failure_class=failure_class,
            project_id=str(task.project_id) if task.project_id else "",
        )


async def _verify_pr_merged(task: Task, session: AsyncSession) -> bool:
    """Check if a create task's PR is merged. Uses context cache, falls back to GitHub API.

    Uses its own session for DB writes to avoid polluting the caller's session state.
    The passed session is only used for reads (artifact lookup).
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.routers.agent import get_github_token_for_task
    from app.github import get_pull_request

    if (task.context or {}).get("pr_merged"):
        return True

    # Negative-result cache: when a batch of sibling children completes in
    # quick succession, every completion event triggers _check_and_trigger_rollup
    # which re-verifies every child.  Without this, 20 children completing at
    # once == 400 PR lookups in a short window.  Module-level dict keyed by
    # task.id — session-independent so the check works regardless of whether
    # the caller commits between fan-out calls.
    _last_negative = _pr_merge_negative_cache.get(task.id)
    if (
        _last_negative is not None
        and (time.time() - _last_negative) < _PR_MERGE_NEGATIVE_TTL_S
    ):
        import app.shared_state as _ss

        _ss.github_cache_hits["pr_merge_negative"] = (
            _ss.github_cache_hits.get("pr_merge_negative", 0) + 1
        )
        return False

    # Look for PR artifact to verify via API
    art_result = await session.execute(
        select(Artifact).where(
            Artifact.task_id == task.id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
        )
    )
    artifact = art_result.scalars().first()
    if not artifact or not artifact.pr_url:
        # No PR artifact — task completed without creating a PR (e.g., no changes needed).
        # Treat as "done" so it doesn't block rollup.
        # Cache in context so partial-rollup path (which checks context.pr_merged directly)
        # doesn't see this task as "unmerged".
        mark_unblocked_non_merge(task, reason="rollup_no_pr_artifact")
        return True
    if artifact and artifact.pr_url:
        # Shape-validate before parsing. Production incident 2026-05-06: a
        # haiku retry whose ./check viewport gate was wedged improvised a
        # `https://github.com/.../compare/main...blueprint/task-<id>` URL into
        # the wrapper's PR-URL marker file; the wrapper trusted it and saved
        # a PULL_REQUEST artifact with that pr_url. Without this guard the
        # int(...) below crashed every rollup cycle (~36x/window) and dirtied
        # the log with full tracebacks. The wrapper now validates too
        # (agent/src/handlers/code-change.ts), but bad rows from before that
        # fix still exist.
        last_seg = artifact.pr_url.rstrip("/").split("/")[-1]
        if "/pull/" not in artifact.pr_url or not last_seg.isdigit():
            logger.warning(
                f"[PR_VERIFY] Task {task.id} has malformed PR artifact "
                f"(pr_url={artifact.pr_url!r}); treating as not-merged"
            )
            _pr_merge_negative_cache[task.id] = time.time()
            return False
        try:
            pr_number = int(last_seg)
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            try:
                pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            except HTTPException as api_err:
                if api_err.status_code not in (401, 500, 502, 503):
                    raise
                if api_err.status_code != 401:
                    # Transient GitHub API failure — can't verify now. Raise the
                    # marker (rather than returning) so the negative cache IS
                    # stamped: without it this dep is re-verified on every ~5s
                    # dispatch cycle, at up to 3 HTTP calls apiece.
                    raise _TransientPRVerifyError(
                        f"GitHub API error ({api_err.status_code}) checking PR "
                        f"#{pr_number} for task {task.id}"
                    ) from api_err
                # Token expired — invalidate cache and retry once with a fresh token
                from app.routers.agent import invalidate_github_token_cache

                invalidate_github_token_cache(str(task.project_id))
                logger.warning(
                    f"[PR_VERIFY] Auth failure for task {task.id} PR #{pr_number} — retrying with fresh token"
                )
                gh_token, owner, repo = await get_github_token_for_task(
                    task.id, session, force_refresh=True
                )
                try:
                    pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
                except HTTPException as retry_err:
                    if retry_err.status_code in (500, 502, 503):
                        # Transient GitHub error on the fresh-token retry.
                        raise _TransientPRVerifyError(
                            f"GitHub API error ({retry_err.status_code}) on "
                            f"fresh-token retry for PR #{pr_number} task {task.id}"
                        ) from retry_err
                    raise
                except Exception as retry_exc:
                    raise _TransientPRVerifyError(
                        f"GitHub fetch error on fresh-token retry for PR "
                        f"#{pr_number} task {task.id}: {retry_exc}"
                    ) from retry_exc
            except Exception as fetch_exc:
                raise _TransientPRVerifyError(
                    f"GitHub fetch error for PR #{pr_number} task {task.id}: "
                    f"{fetch_exc}"
                ) from fetch_exc
            if pr_data.get("merged"):
                project = (
                    await session.get(Project, task.project_id)
                    if task.project_id is not None
                    else None
                )
                merged_base_branch = (pr_data.get("base") or {}).get("ref")
                record_pr_merged = await merged_pr_counts_as_integration_landing(
                    task,
                    project,
                    session,
                    pr_number=pr_number,
                    merged_base_branch=merged_base_branch,
                    source="rollup_pr_verification.verify_pr_merged",
                )
                if record_pr_merged:
                    # Update in-memory first so caller sees the change even if DB write
                    # fails. Pass ``project`` so the PR-verification landing path also
                    # mints the capability proof (not just the acceptance verdict).
                    mark_landed_on_integration(task, project)
                    # Persist to DB in separate session (best-effort — may fail if
                    # caller holds FOR UPDATE lock on this row, but in-memory update is
                    # sufficient for the current rollup cycle).
                    try:
                        async with AsyncSessionLocal() as write_session:
                            db_task = await write_session.get(Task, task.id)
                            if db_task:
                                db_task.context = {
                                    **(db_task.context or {}),
                                    **(task.context or {}),
                                }
                                write_session.add(db_task)
                                await write_session.commit()
                                # Heal the direction's research-history merge counts
                                # now that the landed signal is committed. This
                                # verification path discovers a child's PR landing
                                # asynchronously, well after the child went terminal,
                                # so no status transition re-fires the recompute and
                                # prs_merged would otherwise stay stale-low. Reuse the
                                # write_session (post-commit) so the recompute sees
                                # the merge. No-op without direction lineage.
                                from app.direction_outcomes import (
                                    refresh_direction_outcomes_after_merge,
                                )

                                await refresh_direction_outcomes_after_merge(
                                    db_task, write_session
                                )
                    except Exception as write_err:
                        logger.warning(
                            f"[PR_VERIFY] Failed to persist pr_merged for task {task.id}: {write_err}"
                        )
                return True
            # Detect closed-but-not-merged (rejected/abandoned PR) — don't block rollup forever
            if pr_data.get("state") == "closed" and not pr_data.get("merged"):
                _ctx = task.context or {}
                # Distinguish a DELIBERATE success-close from a rejection. A PR
                # can be closed *because the work was already achieved* — not
                # rejected:
                #   • pr_closed_empty   — the empty/branch-equivalent rollup PR
                #     close (review pipeline) found no diff to merge.
                #   • pr_closed_success — the same change merged via a sibling /
                #     dedup PR ("already merged by another task", set by
                #     ci_completion), or an agent exercised its early-exit right.
                # In those cases the task's desired outcome IS in the target
                # branch, so the child is COMPLETED, not FAILED — failing it
                # mislabels a success and can cascade a rollup parent into a
                # partial-failure finalize. A genuine rejection (review-cap
                # exhausted, manual close) carries neither marker → stays FAILED.
                if _ctx.get("pr_closed_empty") or _ctx.get("pr_closed_success"):
                    logger.info(
                        f"[PR_VERIFY] Task {task.id} PR #{pr_number} closed without "
                        f"merging, but marked as a deliberate success-close (work "
                        f"landed elsewhere) — completing instead of failing"
                    )
                    merged_base_branch = (pr_data.get("base") or {}).get("ref")
                    project = (
                        await session.get(Project, task.project_id)
                        if task.project_id is not None
                        else None
                    )
                    record_pr_merged = await merged_pr_counts_as_integration_landing(
                        task,
                        project,
                        session,
                        pr_number=pr_number,
                        merged_base_branch=merged_base_branch,
                        source="rollup_pr_verification.success_close",
                    )
                    task.context = dict(_ctx)
                    if record_pr_merged:
                        mark_landed_on_integration(task, project)
                    if task.status != TaskStatus.COMPLETED:
                        task.status = TaskStatus.COMPLETED
                    try:
                        async with AsyncSessionLocal() as write_session:
                            db_task = await write_session.get(Task, task.id)
                            if db_task:
                                if record_pr_merged:
                                    db_task.context = {
                                        **(db_task.context or {}),
                                        **(task.context or {}),
                                    }
                                if db_task.status != TaskStatus.COMPLETED:
                                    db_task.status = TaskStatus.COMPLETED
                                write_session.add(db_task)
                                await write_session.commit()
                                # A deliberate success-close that records an
                                # integration landing must heal the direction's
                                # merge counts too (same async-late-landing gap as
                                # the merged branch above). Reuse the post-commit
                                # write_session. No-op without direction lineage.
                                if record_pr_merged:
                                    from app.direction_outcomes import (
                                        refresh_direction_outcomes_after_merge,
                                    )

                                    await refresh_direction_outcomes_after_merge(
                                        db_task, write_session
                                    )
                    except Exception as _sc_err:
                        logger.warning(
                            f"[PR_VERIFY] Failed to persist success-close for task "
                            f"{task.id}: {_sc_err}"
                        )
                    return True
                # FIX A: a closed-unmerged PR with NO genuine review rejection
                # (no CHANGES_REQUESTED review, no pr_rejected/review_cap_exhausted
                # marker) AND a recognized cleanup/supersession signal (approved-
                # then-closed, supersession/de-scope marker, or Blueprint-bot
                # authorship) is housekeeping, not a failure. Land it CANCELLED
                # with failure_detail='pr_closed_non_rejection' and skip the
                # failure alert so a non-failure neither dings direction health
                # nor pages the operator. GUARDRAIL: _pr_closed_is_non_rejection
                # returns False on ANY genuine-rejection signal or absence of a
                # cleanup signal, so the genuine FAILED path below is preserved.
                # Imported locally: rollup_management imports this module at load
                # time, so a top-level import here would be circular.
                from app.rollup_management import _pr_closed_is_non_rejection

                if await _pr_closed_is_non_rejection(
                    task, pr_data, pr_number, session
                ):
                    logger.info(
                        f"[PR_VERIFY] Task {task.id} PR #{pr_number} closed without "
                        f"merging with no genuine rejection — treating as a "
                        f"cleanup/supersession close (CANCELLED, no alert)"
                    )
                    from app.failure_classification import (
                        SCOPE_EMPTY,
                        persist_failure_category,
                    )

                    task.context = {
                        **(task.context or {}),
                        "pr_closed_unmerged": True,
                        "failure_detail": "pr_closed_non_rejection",
                    }
                    task.status = TaskStatus.CANCELLED
                    task.latest_status_message = (
                        f"PR #{pr_number} closed without merging "
                        f"(no rejection — cleanup/supersession)"
                    )
                    # SCOPE_EMPTY-class: the desired change landed elsewhere or
                    # the PR was de-scoped — an INFRA/no-op class, not a code
                    # defect. The failure_detail marker is what direction metrics
                    # key on (direction_outcomes._is_non_genuine_cancelled).
                    persist_failure_category(task, SCOPE_EMPTY)
                    try:
                        async with AsyncSessionLocal() as write_session:
                            db_task = await write_session.get(Task, task.id)
                            if db_task:
                                db_task.context = {
                                    **(db_task.context or {}),
                                    "pr_closed_unmerged": True,
                                    "failure_detail": "pr_closed_non_rejection",
                                    "failure_category": SCOPE_EMPTY,
                                }
                                db_task.status = TaskStatus.CANCELLED
                                db_task.latest_status_message = (
                                    f"PR #{pr_number} closed without merging "
                                    f"(no rejection — cleanup/supersession)"
                                )
                                write_session.add(db_task)
                                await write_session.commit()
                    except Exception as _nr_err:
                        logger.warning(
                            f"[PR_VERIFY] Failed to persist pr_closed_non_rejection "
                            f"for task {task.id}: {_nr_err}"
                        )
                    # Skip _maybe_create_failure_alert by design — non-genuine.
                    return True
                logger.warning(
                    f"[PR_VERIFY] Task {task.id} PR #{pr_number} was closed without merging — marking as rejected"
                )
                # Update in-memory first — set message AND status together so the
                # outer session's commit always carries the explanatory message,
                # even if the separate write_session below fails (row lock, etc.).
                task.context = {**(task.context or {}), "pr_closed_unmerged": True}
                task.status = TaskStatus.FAILED
                task.latest_status_message = f"PR #{pr_number} closed without merging"
                from app.failure_classification import (
                    classify_failure,
                    persist_failure_category,
                )

                _fc = classify_failure(task)
                persist_failure_category(task, _fc)
                _record_rollup_failure_signals(task, _fc)
                try:
                    async with AsyncSessionLocal() as write_session:
                        db_task = await write_session.get(Task, task.id)
                        if db_task:
                            db_task.context = {
                                **(db_task.context or {}),
                                "pr_closed_unmerged": True,
                                "failure_category": _fc,
                            }
                            db_task.status = TaskStatus.FAILED
                            db_task.latest_status_message = (
                                f"PR #{pr_number} closed without merging"
                            )
                            write_session.add(db_task)
                            await _maybe_create_failure_alert(
                                db_task,
                                write_session,
                                f"PR #{pr_number} was closed without merging",
                            )
                            await write_session.commit()
                except Exception as write_err:
                    logger.warning(
                        f"[PR_VERIFY] Failed to persist pr_closed_unmerged for task {task.id}: {write_err}"
                    )
                return True  # Return True so callers don't block — task is now FAILED which they handle
        except _TransientPRVerifyError as transient_err:
            # A transient GitHub failure falls through to the negative-cache stamp
            # below (see _TransientPRVerifyError): the un-verified dep must be
            # throttled for the TTL like any other not-yet-merged result, or the
            # dispatch loop re-hammers GitHub every ~5s while it is already
            # degraded. No traceback — this class is expected and self-healing.
            logger.warning(
                f"[PR_VERIFY] {transient_err} — skipping (transient, "
                f"negative-cached for {_PR_MERGE_NEGATIVE_TTL_S}s)"
            )
        except Exception as e:
            logger.warning(
                f"[PR_VERIFY] Failed to verify PR merge for task {task.id}: {e}",
                exc_info=True,
            )
    # Stamp the negative cache so the next caller within the TTL skips the
    # GitHub round-trip.  Lazy eviction: keep it lightweight, drop entries
    # that are clearly stale whenever we detect the dict is oversized.
    _pr_merge_negative_cache[task.id] = time.time()
    if len(_pr_merge_negative_cache) > 2000:
        cutoff = time.time() - (_PR_MERGE_NEGATIVE_TTL_S * 4)
        for k in [k for k, ts in _pr_merge_negative_cache.items() if ts < cutoff]:
            _pr_merge_negative_cache.pop(k, None)
    return False
