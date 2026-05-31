"""CI completion handler — shared between webhook and polling fallback.

Processes CI check results for tasks waiting on CI. Handles merge on success,
fix spawning on failure, and various edge cases (merge conflicts, race conditions).

Merge gate
==========
A PR is merged only when BOTH gates are open on the parking task's review_loop:

  - ``ci_passed: True``      — CI reported success (set by handle_ci_completion)
  - ``review_approved: True`` — A review task signed off (set by task_lifecycle)

Whichever signal arrives first sets its flag and waits; the second flips the
gate and triggers the merge. ``ci_waiting`` only means "orchestrator should poll
CI" — it is NOT permission to merge. The legacy ``verdict == "APPROVE"`` form is
accepted as a fallback so in-flight tasks from before the gate split still merge.
"""

import asyncio
import contextlib
import logging
import re
import uuid

from sqlalchemy import func
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Task, TaskStatus, _utcnow
from app.vm_cost_rates import get_vm_rate_per_second

logger = logging.getLogger(__name__)


def _pr_number_match(session, pr_number: int):
    """Dialect-conditional WHERE clause for payloads[0].pr_number filter.

    Postgres: matches the ``ix_task_payloads_pr_number`` expression index
    (alembic 0057) via ``.as_string()`` which compiles to ``-> 0 ->> 'pr_number'``.
    SQLite: uses ``func.json_extract`` because the .as_string() form
    JSON_QUOTE-double-quotes ints on SQLite and never matches.
    See review_pipeline.py:_spawn_pipeline_task for the canonical pattern.
    """
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    if dialect_name == "sqlite":
        return func.json_extract(Task.payloads, "$[0].pr_number") == pr_number
    return Task.payloads[0]["pr_number"].as_string() == str(pr_number)


# Hard cap on merge retries — scope-aware. Small-scope PRs (single-file tweaks)
# have fewer legitimate reasons for transient merge failures, so cap sooner.
# Escalation alert fires at (cap - 5), final-warn at (cap - 2).
MERGE_FAILURE_HARD_CAP = 15
_SCOPE_MERGE_HARD_CAP = {"small": 8, "medium": 12, "large": 15}


def _merge_hard_cap_for_task(task: Task) -> int:
    """Return scope-adjusted merge hard cap, falling back to the default."""
    ctx = task.context or {}
    scope = ctx.get("scope")
    if not scope:
        payload = task.first_payload or {}
        scope = payload.get("scope")
    if isinstance(scope, str):
        return _SCOPE_MERGE_HARD_CAP.get(scope.lower(), MERGE_FAILURE_HARD_CAP)
    return MERGE_FAILURE_HARD_CAP


_MERGE_ERR_NORMALIZE_PATTERNS = (
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<UUID>",
    ),
    (re.compile(r"\b[0-9a-fA-F]{12,}\b"), "<HEX>"),
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        "<TS>",
    ),
    (re.compile(r"\b\d{10,}\b"), "<N>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def _normalize_merge_error(msg: str) -> str:
    """Strip variable data (IDs, timestamps, counts) so streak detection sees
    structurally-identical errors as equal across retries."""
    if not msg:
        return ""
    out = msg
    for pattern, repl in _MERGE_ERR_NORMALIZE_PATTERNS:
        out = pattern.sub(repl, out)
    return out.strip().lower()


# Deterministically non-retryable merge errors. Retrying these cannot succeed
# without external state change (corrupt refs, GitHub-level blocks), so we skip
# the usual 2-streak requirement and exit on first occurrence.
_DETERMINISTIC_MERGE_ERROR_PATTERNS = (
    "head sha can't be blank",
    "pull request is not mergeable",
    "base branch was modified",
)


def _is_deterministic_merge_error(normalized_msg: str) -> bool:
    if not normalized_msg:
        return False
    return any(pat in normalized_msg for pat in _DETERMINISTIC_MERGE_ERROR_PATTERNS)


def _review_approved(review_loop: dict) -> bool:
    """True if a review task signed off on this PR.

    Accepts the new ``review_approved`` flag and the legacy ``verdict == "APPROVE"``
    form so EVALUATING tasks written before the gate split still merge.
    """
    if review_loop.get("review_approved"):
        return True
    return review_loop.get("verdict") == "APPROVE"


async def _post_merge_summary(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    task: Task,
    review_loop: dict,
) -> None:
    """Post a one-time structured merge summary on a Blueprint-generated PR. Best-effort."""
    from app.github import create_pr_comment

    ctx = task.context or {}
    payload = task.first_payload or {}

    task_type = payload.get("type") or "create"
    scope = ctx.get("scope") or payload.get("scope")
    iteration = review_loop.get("iteration", 0)
    direction_title = ctx.get("direction_title")
    direction_id = ctx.get("direction_id")
    provider = ctx.get("llm_provider")
    model = ctx.get("llm_model")

    cost_usd = None
    _prov_at = getattr(task, "vm_provisioned_at", None)
    _stop_at = getattr(task, "vm_stopped_at", None)
    if _prov_at and _stop_at:
        _rate = get_vm_rate_per_second(spot=getattr(task, "vm_spot", False))
        cost_usd = (_stop_at - _prov_at).total_seconds() * _rate

    type_scope = f"{task_type} / {scope}" if scope else task_type
    lines = [f"**Blueprint merge summary** — {type_scope}", ""]
    lines.append(f"- **Review-fix iterations**: {iteration}")
    agent_parts = [p for p in [provider, model] if p]
    if agent_parts:
        lines.append(f"- **Agent**: {' / '.join(agent_parts)}")
    if cost_usd is not None:
        lines.append(f"- **Estimated cost**: ~${cost_usd:.2f}")
    if direction_title or direction_id:
        lines.append(f"- **Direction**: {(direction_title or str(direction_id))[:80]}")

    try:
        await create_pr_comment(gh_token, owner, repo, pr_number, "\n".join(lines))
    except Exception as exc:
        logger.warning(f"[CI] Failed to post merge summary on PR #{pr_number}: {exc}")


# Round 4 W1 (audit C3): content snapshot used by the merge handler's
# lock-window-break. Heartbeat-driven ``updated_at`` advances every 30s,
# so the prior ``updated_at`` validation would false-defer under load.
# Validate against the merge-gate fields instead.
def _capture_merge_snapshot(task: Task) -> dict:
    """Snapshot the merge-gate fields lock-window-break protects.

    Fields covered:
      - status: must remain the same (typically EVALUATING) for the merge
        path to apply without overwriting a peer's terminal transition
      - review_loop.iteration: review-fix iteration counter
      - review_loop.review_approved / verdict: the review gate flag
      - review_loop.ci_passed: the CI gate flag
    Heartbeat-only updates (last_heartbeat, last_metrics_json,
    heartbeat_count) do NOT touch any of these, so they don't trip the
    validator under heartbeat overlap.
    """
    ctx = task.context or {}
    review_loop = ctx.get("review_loop") or {}
    return {
        "status": task.status,
        "review_loop_iteration": review_loop.get("iteration"),
        "review_loop_review_approved": review_loop.get("review_approved"),
        "review_loop_verdict": review_loop.get("verdict"),
        "review_loop_ci_passed": review_loop.get("ci_passed"),
    }


async def _relock_merge_task(
    session: AsyncSession,
    task_id,
    expected_snapshot: dict,
    cached_task: Task | None,
    *,
    source: str,
) -> Task | None:
    """Re-acquire FOR UPDATE on the merge task and validate the pre-release snapshot.

    Mirrors ``rollup_management._relock_parent`` (the canonical reference). Returns
    the freshly-loaded Task if the row is still ours and the merge-gate snapshot
    still matches, or None if the row is held by a peer or has been mutated during
    the unlocked window — in which case the caller should defer to the next sweep
    rather than overwrite a peer's terminal transition.

    Why ``session.expire`` first: ``populate_existing=True`` alone has been observed
    unreliable under async + ``with_for_update(skip_locked=True)`` (see
    rollup_management.py:155 and task_completion.py:1671). Expiring the cached
    instance guarantees the re-SELECT cannot return stale identity-map attributes.
    """
    if cached_task is not None:
        try:
            session.expire(cached_task)
        except InvalidRequestError:
            # Object detached from this session — fall back to expire_all so the
            # populate_existing SELECT below can repopulate from a clean slate.
            session.expire_all()
    relock_stmt = (
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    fresh = (await session.execute(relock_stmt)).scalars().first()
    if fresh is None:
        logger.info(
            f"[CI] Task {task_id} held by peer after merge call — "
            f"deferring to next sweep ({source})"
        )
        return None
    fresh_snapshot = _capture_merge_snapshot(fresh)
    if fresh_snapshot != expected_snapshot:
        logger.info(
            f"[CI] Task {task_id} mutated during merge window "
            f"(expected snapshot={expected_snapshot}, found {fresh_snapshot}) — "
            f"deferring to next sweep ({source})"
        )
        return None
    return fresh


async def _attempt_merge_with_retry_and_escalation(
    task: Task,
    gh_token: str,
    owner: str,
    repo: str,
    session: AsyncSession,
    *,
    source: str,
) -> bool:
    """Both gates are open — try to merge. Mutates ``task`` in place; caller commits.

    Handles the existing retry / rule-block escalation / merge-failure path that
    used to live inline inside handle_ci_completion. Returns True iff merged.

    Round 3 H4 (lock-window break): caller (process_ci_completion) holds the
    task FOR UPDATE lock since line 762. The merge_pull_request call and its
    retry loop (5 + 15 + 45 = 65s combined) hold that lock across long-running
    GitHub I/O, starving concurrent heartbeats on the same row. We mirror the
    rollup_management ``_release_parent_lock`` / ``_relock_parent`` pattern:
    capture a content snapshot, commit-release, run the merge calls without
    lock, then re-acquire with snapshot validation. If a peer mutated the row
    during the unlocked window, the re-acquire fails and we defer.

    Round 4 W1 (audit C1, C3): the prior implementation snapshot ``updated_at``
    and validated against it on re-acquire. Two structural bugs:
      - C1 (cache): SQLAlchemy's identity map under ``expire_on_commit=False``
        returned the cached pre-commit instance on re-SELECT, so
        ``fresh_task.updated_at`` always equalled ``pre_merge_updated_at`` and
        the validation never fired. Fix: ``populate_existing=True`` on the
        re-SELECT forces SQLA to refresh attributes from the new row.
      - C3 (heartbeat): even with the cache fix, ``onupdate=_utcnow``
        advances ``updated_at`` on every heartbeat. Under load every
        merge attempt would false-defer. Fix: validate against a content
        snapshot of the fields the merge window protects (status,
        review_loop.iteration, review_loop.review_approved/verdict,
        ci_passed) — heartbeat-only mutations don't touch those fields.
    """
    from app.github import merge_pull_request, create_pr_comment, get_pull_request
    from app.main import _maybe_create_failure_alert
    from app.models import Project
    from app.shared_state import get_project_integration_branch

    ci_loop = (task.context or {}).get("review_loop", {})
    ci_pr_number = ci_loop.get("pr_number")
    ci_payload = task.first_payload
    ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get(
        "original_task_id", ""
    )
    ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
    _merge_project = (
        await session.get(Project, task.project_id) if task.project_id else None
    )
    ci_base = (
        ci_loop.get("base_branch", "")
        or ci_payload.get("base_branch", "")
        or get_project_integration_branch(_merge_project)
    )

    # Round 4 W1: capture a content snapshot BEFORE releasing the lock.
    # ``parent_id_local`` mirrors the snapshot-stale convention from
    # rollup_management.py: the in-memory ``task`` is stale after release;
    # only stable identifiers (id, project_id) and the captured snapshot
    # are safe to read between release and re-acquire.
    #
    # The snapshot covers fields the merge gate protects from peer mutation:
    #   - status: must remain EVALUATING for the merge to apply
    #   - review_loop.iteration: review-fix iteration counter; advances if
    #     a fix-completion handler has already moved on
    #   - review_loop.review_approved / verdict: the gate flag itself; if
    #     a peer (e.g. APPROVE handler) cleared/changed it we must defer
    #   - review_loop.ci_passed: the other gate; same logic
    # ``updated_at`` is intentionally NOT in the snapshot — heartbeat
    # ``onupdate=_utcnow`` advances it every 30s and would false-defer
    # the merge under load (audit C3).
    parent_id_local = task.id
    pre_merge_snapshot = _capture_merge_snapshot(task)
    # R4-W1 (audit C1): explicitly mark ``context`` dirty before the commit.
    # The merge-failure path mutates ``task.context`` via full reassignment
    # AFTER the prior populate_existing reset SQLA's change-tracking; in
    # some SQLA states the post-populate assignment is not detected as
    # dirty (verified empirically against test_merge_failure_final_warn:
    # without flag_modified, the second invocation's commit failed to
    # persist the first invocation's dirty mutations). ``flag_modified``
    # is a no-op when the attribute is already tracked, but corrects the
    # state when it isn't.
    flag_modified(task, "context")
    session.add(task)
    await session.commit()  # release task FOR UPDATE lock; SQLA returns
    # the connection to the pool until the next
    # operation auto-acquires (audit H6 — no
    # connection held during the GitHub window).

    merge_result = await merge_pull_request(gh_token, owner, repo, ci_pr_number)
    if (
        not merge_result.get("merged")
        and not merge_result.get("conflict")
        and source != "webhook"
    ):
        # Retry with backoff (skip in webhook context — would timeout delivery).
        # Skip retries on conflict: 409 means the diff can't merge until code changes.
        for attempt in range(3):
            await asyncio.sleep(5 * (3**attempt))
            merge_result = await merge_pull_request(gh_token, owner, repo, ci_pr_number)
            if merge_result.get("merged") or merge_result.get("conflict"):
                break

    # ── PRE-RELOCK GITHUB I/O ──────────────────────────────────────────────
    # Round 5 W1 (this commit): all GitHub API calls below this point used to
    # run with the task FOR UPDATE lock held (re-acquired after merge_pull_request).
    # That meant 30-120s of held-lock during create_pr_comment + _post_merge_summary
    # (merged path), get_pull_request (recheck), and update_branch (behind path) —
    # blocking concurrent heartbeats and tripping idle_in_transaction_session_timeout.
    # Production 2026-05-04: same incident shape as agent_status.py:1430 cascade.
    #
    # New structure: do all remaining GitHub I/O while still UNLOCKED, decide the
    # outcome, then re-acquire the lock only to apply the final DB mutation.
    # ──────────────────────────────────────────────────────────────────────

    merge_msg = merge_result.get("message", "")
    _in_progress = (
        "in progress" in merge_msg.lower() or "status check" in merge_msg.lower()
    )

    # If the merge succeeded, post the cosmetic GitHub artefacts (merge banner +
    # summary comment) BEFORE re-acquiring the lock. We use the cached, stale
    # ``task`` instance for _post_merge_summary's read-only field access; the
    # post-relock task assignment will pick up any peer mutation if it happened.
    #
    # Idempotency gate (security audit MEDIUM-1, 2026-05-04): two concurrent
    # merge sweeps (e.g. webhook + CI poll) can both observe merged=True from
    # GitHub (idempotent on the second caller) and would otherwise BOTH post
    # the cosmetic comment + summary, producing duplicates on the PR. Gate on
    # task.context["_merge_comment_posted"] — set inside the relock window
    # below — so the second caller skips. Best-effort; if the relock fails
    # before flagging, a duplicate is still possible but the next caller's
    # gate will catch the third.
    if merge_result.get("merged") and not (task.context or {}).get(
        "_merge_comment_posted"
    ):
        try:
            await create_pr_comment(
                gh_token,
                owner,
                repo,
                ci_pr_number,
                "**Blueprint CI + Review**: All checks passed, PR auto-merged.",
            )
        except Exception as e:
            logger.warning(
                f"[CI] create_pr_comment after merge failed for PR #{ci_pr_number}: {e}",
                exc_info=True,
            )
        try:
            await _post_merge_summary(
                gh_token, owner, repo, ci_pr_number, task, ci_loop
            )
        except Exception as e:
            logger.warning(
                f"[CI] _post_merge_summary failed for PR #{ci_pr_number}: {e}",
                exc_info=True,
            )

    # If the merge failed and we're not in the "checks in progress" early-exit
    # path, fetch the PR state to drive escalation logic. Done unlocked so the
    # GitHub round-trip (typ. 1-30s, up to 120s on cutover) doesn't hold FOR UPDATE.
    _recheck_pr: dict | None = None
    _recheck_err: BaseException | None = None
    _ms: str = ""
    _upd: dict | None = None
    if not merge_result.get("merged") and not _in_progress:
        try:
            _recheck_pr = await get_pull_request(gh_token, owner, repo, ci_pr_number)
            _ms = ((_recheck_pr or {}).get("mergeable_state") or "").lower()
        except Exception as e:
            _recheck_err = e
            logger.warning(
                f"[CI] get_pull_request recheck failed for PR #{ci_pr_number}: {e}",
                exc_info=True,
            )

        # 'behind' path: call update_branch (GitHub write) BEFORE re-acquiring
        # the lock. The result is consumed in the lock-held block below.
        if _recheck_err is None and _ms == "behind":
            from app.github import pr_is_not_behind_for, update_branch

            _not_behind = await pr_is_not_behind_for(
                gh_token,
                owner,
                repo,
                pr_detail=_recheck_pr,
                base_branch=ci_base,
                call_site="ci_completion",
            )
            if _not_behind:
                _upd = {"not_behind": True}
                logger.info(
                    f"[CI] PR #{ci_pr_number} mergeable_state='behind' but "
                    f"pr_is_not_behind=True — skipping update_branch"
                )
            else:
                try:
                    _upd = await update_branch(gh_token, owner, repo, ci_pr_number)
                except Exception as e:
                    _upd = {"updated": False, "message": str(e)[:200]}
                    logger.warning(f"[CI] PR #{ci_pr_number} update_branch raised: {e}")
                else:
                    # Tag the canonical "no new commits on the base branch" 422
                    # so the existing `_upd.get("not_behind")` short-circuit in
                    # the merge-failure branch below treats it as a stale-cache
                    # signal rather than a transient failure that increments
                    # update_branch_failures.
                    from app.github import _tag_not_behind_if_no_new_commits

                    _upd = _tag_not_behind_if_no_new_commits(_upd)

    # ── RE-ACQUIRE FOR UPDATE FOR FINAL DB MUTATIONS ──────────────────────
    # All GitHub I/O above ran with no row lock held. Now re-acquire briefly
    # to apply state mutations (status, context, latest_status_message) and
    # any spawn calls (which do DB work but no further GitHub I/O).
    fresh_task = await _relock_merge_task(
        session,
        parent_id_local,
        pre_merge_snapshot,
        task,
        source=source,
    )
    if fresh_task is None:
        return bool(merge_result.get("merged"))
    # Re-bind ``task`` to the fresh, locked instance — the original is stale.
    task = fresh_task
    ci_loop = (task.context or {}).get("review_loop", {})

    if merge_result.get("merged"):
        # GitHub artefacts already posted (above). Apply DB state under the lock.
        task.status = TaskStatus.COMPLETED
        task.latest_status_message = (
            f"CI passed + review approved, PR #{ci_pr_number} merged ({source})"
        )
        # Idempotency flag for the cosmetic comment post — guards a concurrent
        # merge sweep from posting duplicates (security audit MEDIUM-1).
        task.context = {**(task.context or {}), "_merge_comment_posted": True}
        flag_modified(task, "context")
        if ci_orig_id:
            # C1: the pr_merged write on the ORIGINAL (rollup-orig) task is an
            # independent operation that must NOT share this merge-task
            # ``session``. Two failure modes the old shared-session code had:
            #   (1) a raise here (e.g. session.get hitting a DB error) left the
            #       caller's session in PendingRollback, so the caller's commit
            #       of task.status=COMPLETED silently failed or 500'd; and
            #   (2) the function returned True regardless, so the caller treated
            #       the merge as fully done while the rollup-unblocking
            #       ``pr_merged`` flag was actually dropped — the rollup that
            #       gates on that flag then defers indefinitely.
            # Give it its OWN AsyncSessionLocal scope (mirrors
            # maybe_finalize_qualification / reaper Step 9): it commits the flag
            # independently and an error here is fully contained.
            try:
                from app.database import AsyncSessionLocal

                async with AsyncSessionLocal() as _orig_session:
                    orig = await _orig_session.get(Task, uuid.UUID(ci_orig_id))
                    if orig:
                        orig.context = {**(orig.context or {}), "pr_merged": True}
                        if orig.status == TaskStatus.EVALUATING and not (
                            orig.context or {}
                        ).get("review_loop", {}).get("ci_waiting"):
                            orig.status = TaskStatus.COMPLETED
                            orig.latest_status_message = "Rollup PR merged"
                        _orig_session.add(orig)
                        await _orig_session.commit()
            except Exception as e:
                logger.warning(
                    f"[CI] Failed to set pr_merged on {ci_orig_id}: {e}", exc_info=True
                )
        logger.info(f"[CI] PR #{ci_pr_number} merged ({source})")
        return True

    if _in_progress:
        from app import pause_clock
        from app.shared_state import runtime_config as _rt_cfg

        # Cap repeated ``ci_wait_started`` resets so a PR whose required checks
        # never resolve cannot spin on this path until the 6-hour parent
        # timeout fires.  Each reset bumps the CI-wait deadline by another
        # full backstop window; without a cap the orchestrator polls forever
        # while logging "in progress" responses every cycle.  After the cap
        # we stop resetting (still log + persist the last-merge state) so the
        # normal CI backstop runs out on schedule and the parent fails with a
        # message that names the real blocker.
        _reset_cap = int(_rt_cfg.get("ci_in_progress_reset_cap", 3))
        _resets_so_far = int(ci_loop.get("merge_in_progress_resets", 0))
        # Persist the merge-side diagnostics so the EVALUATING-timeout reason-
        # picker in services.py can surface the real blocker instead of the
        # historical "both gates unmet" lie.
        ci_loop["mergeable_state"] = _ms or "unknown"
        ci_loop["last_merge_msg"] = merge_msg
        if _resets_so_far < _reset_cap:
            ci_loop["ci_poll_interval"] = 10
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            ci_loop["ci_wait_started"] = _utcnow().isoformat()
            ci_loop["ci_wait_pause_baseline"] = await pause_clock.current_baseline(
                session
            )
            ci_loop["merge_in_progress_resets"] = _resets_so_far + 1
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            flag_modified(task, "context")
            task.latest_status_message = (
                f"Merge blocked (checks re-running on PR #{ci_pr_number}) — back to CI polling "
                f"({_resets_so_far + 1}/{_reset_cap})"
            )
            logger.warning(
                f"[CI] PR #{ci_pr_number} merge blocked by in-progress checks — "
                f"returning to CI wait ({source}, reset {_resets_so_far + 1}/{_reset_cap})"
            )
        else:
            # Cap reached — stop resetting the CI-wait clock.  The normal
            # backstop runs out on schedule and services.py surfaces the
            # mergeable_state / last_merge_msg in the timeout reason.
            ci_loop["ci_poll_interval"] = 30
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            flag_modified(task, "context")
            task.latest_status_message = (
                f"Merge blocked on PR #{ci_pr_number} (in-progress reset cap "
                f"{_reset_cap} reached) — letting backstop run out"
            )
            logger.warning(
                f"[CI] PR #{ci_pr_number} merge in-progress reset cap "
                f"({_reset_cap}) reached — ci_wait_started no longer being reset "
                f"({source})"
            )
        return False

    try:
        # If get_pull_request raised in the unlocked window, surface that as
        # the recheck-failed branch (mirrors the original except-clause behavior).
        if _recheck_err is not None:
            raise _recheck_err
        if _recheck_pr and (
            _recheck_pr.get("merged") or _recheck_pr.get("state") == "closed"
        ):
            task.status = TaskStatus.COMPLETED
            # Mark a deliberate success-close so the rollup's _verify_pr_merged
            # doesn't later re-fetch this now-closed PR, see closed-not-merged,
            # and downgrade this COMPLETED child back to FAILED (the work landed
            # via the other task's PR — desired outcome achieved).
            task.context = {**(task.context or {}), "pr_closed_success": True}
            flag_modified(task, "context")
            task.latest_status_message = (
                f"PR #{ci_pr_number} already merged by another task"
            )
            logger.info(
                f"[CI] PR #{ci_pr_number} was already merged — marking complete ({source})"
            )
            return True
        # Detect non-transient merge blocks — escalate immediately instead of retrying.
        # Prefer mergeable_state from the refetched PR as the primary signal: it is
        # authoritative and resilient to GitHub API error-message wording changes.
        # Fall back to keyword matching only when mergeable_state is absent or still
        # 'unknown' (GitHub hasn't finished computing it).
        if _ms and _ms != "unknown":
            _is_rule_block = _ms == "blocked"
        else:
            _merge_msg_lower = merge_msg.lower()
            _is_rule_block = any(
                kw in _merge_msg_lower
                for kw in [
                    "rule violation",
                    "branch protection",
                    "required review",
                    "required status",
                    "merge restrictions",
                    "not allowed to merge",
                    "at least",
                    "approving review",
                ]
            )
        # 'unstable': required status checks are failing or still pending.
        # This is not a merge infrastructure failure — defer back to CI polling
        # without burning the merge retry budget.
        if _ms == "unstable":
            from app import pause_clock

            ci_loop["ci_passed"] = False
            ci_loop["ci_poll_interval"] = 30
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            ci_loop["ci_wait_started"] = _utcnow().isoformat()
            ci_loop["ci_wait_pause_baseline"] = await pause_clock.current_baseline(
                session
            )
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            flag_modified(task, "context")
            task.latest_status_message = (
                f"PR #{ci_pr_number} mergeable_state='unstable' (checks pending/failing) "
                f"— re-entering CI polling"
            )
            logger.info(
                f"[CI] PR #{ci_pr_number} unstable — deferring to CI polling ({source})"
            )
            return False

        # 'behind': PR branch is behind the base. update_branch was already
        # called in the unlocked window above; ``_upd`` holds the result.
        if _ms == "behind":
            assert _upd is not None  # set in unlocked block when _ms == "behind"
            from app import pause_clock

            ci_loop["ci_passed"] = False
            ci_loop["ci_poll_interval"] = 30
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            ci_loop["ci_wait_started"] = _utcnow().isoformat()
            ci_loop["ci_wait_pause_baseline"] = await pause_clock.current_baseline(
                session
            )
            if _upd.get("not_behind"):
                # pr_is_not_behind confirmed head already contains base tip —
                # skip update_branch, re-enter CI polling for a fresh merge attempt.
                ci_loop["update_branch_failures"] = 0
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                flag_modified(task, "context")
                task.latest_status_message = (
                    f"PR #{ci_pr_number} reports 'behind' but head already contains"
                    f" base tip — re-entering CI polling without update"
                )
                logger.info(
                    f"[CI] PR #{ci_pr_number} not-behind — skipped update_branch ({source})"
                )
                return False
            if _upd.get("updated"):
                ci_loop["update_branch_failures"] = 0
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                flag_modified(task, "context")
                task.latest_status_message = f"PR #{ci_pr_number} branch behind base — update queued, re-entering CI polling"
                logger.info(
                    f"[CI] PR #{ci_pr_number} behind base — update_branch queued ({source})"
                )
                return False
            _ub_failures = ci_loop.get("update_branch_failures", 0) + 1
            ci_loop["update_branch_failures"] = _ub_failures
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            flag_modified(task, "context")
            _hard_cap_ub = _merge_hard_cap_for_task(task)
            _merge_failures_so_far = ci_loop.get("merge_failures", 0)
            _ub_threshold = max(1, min(3, _hard_cap_ub - _merge_failures_so_far))
            if _ub_failures < _ub_threshold:
                task.latest_status_message = (
                    f"PR #{ci_pr_number} branch behind base — update_branch failed "
                    f"({_ub_failures}/{_ub_threshold}): {(_upd.get('message') or '')[:120]} — re-entering CI polling"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} behind base — update_branch failed "
                    f"({_ub_failures}/{_ub_threshold}): {_upd.get('message')} ({source})"
                )
                return False
            # update_branch failures exceeded dynamic threshold — escalate to fix task with rebase.
            from app.review_pipeline import _spawn_pipeline_task
            from app.rollup_state import _is_rollup_parent_awaiting_merge

            # Phase F3 (H5): push the per-PR filter into SQL using
            # cast(..., Integer) so the ``ix_task_payloads_pr_number`` partial
            # expression index (alembic 0057) can serve the predicate on
            # Postgres. SQLite seq-scans (test path); Postgres uses the index.
            # Matches the ``cast(Task.payloads[0]["pr_number"], Integer)`` form
            # used in review_pipeline.py:_spawn_pipeline_task to keep both call
            # sites dialect-symmetric. Include ``status_log`` in load_only:
            # the Task event listeners (models.py ~360) read it on every
            # status assignment under async sessions, and an unloaded value
            # triggers MissingGreenlet on downstream mutations even if this
            # site only filters the result set. Drop heavy plan_output JSONB.
            active_tasks = (
                (
                    await session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.context,
                                Task.payloads,
                                Task.project_id,
                                Task.status_log,
                            )
                        )
                        .where(
                            Task.project_id == task.project_id,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                ]
                            ),
                            Task.archived.is_(False),
                            _pr_number_match(session, ci_pr_number),
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_fix = next(
                (
                    t
                    for t in active_tasks
                    if t.id != task.id and t.first_payload.get("type") == "fix"
                ),
                None,
            )
            if existing_fix:
                if not _is_rollup_parent_awaiting_merge(task.context):
                    task.status = TaskStatus.COMPLETED
                task.latest_status_message = (
                    f"PR #{ci_pr_number} behind base, update_branch failed {_ub_failures}x, "
                    f"fix already active → {existing_fix.id}"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} behind base escalation — fix already exists → "
                    f"{existing_fix.id} ({source})"
                )
                return False
            behind_prompt = (
                f"PR #{ci_pr_number} is behind `{ci_base}` and GitHub's update_branch API "
                f"has failed {_ub_failures} consecutive times. The code review is already approved — "
                f"focus only on rebasing the branch.\n\n"
                f"After cloning:\n"
                f"  1. Run `git fetch origin {ci_base} && git rebase origin/{ci_base}`.\n"
                f"  2. Resolve any conflicts that surface during the rebase.\n"
                f"  3. Re-run lint/type/test suites for the affected files.\n"
                f"  4. Force-push with `--force-with-lease`.\n\n"
                f"Last update_branch error: {(_upd.get('message') or '')[:500]}"
            )
            fix_task, _ = await _spawn_pipeline_task(
                task,
                "fix",
                session,
                ci_pr_number,
                ci_branch,
                ci_base,
                fix_prompt=behind_prompt,
                original_task_id=ci_orig_id,
                fix_reason="update_branch_failed",
                # Phase 2d: post-APPROVE — code review already approved, so the
                # warm chain has ended. The new fix must cold-provision rather
                # than (potentially) reuse a still-warm reviewer VM.
                suppress_warm_handoff=True,
            )
            if fix_task:
                ci_loop["ci_waiting"] = False
                ci_loop["ci_fix_spawned"] = str(fix_task.id)
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                flag_modified(task, "context")
                if not _is_rollup_parent_awaiting_merge(task.context):
                    task.status = TaskStatus.COMPLETED
                task.latest_status_message = (
                    f"PR #{ci_pr_number} behind base, update_branch failed {_ub_failures}x "
                    f"— spawned fix → {fix_task.id}"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} behind base escalation — spawned fix → "
                    f"{fix_task.id} ({source})"
                )
            else:
                task.latest_status_message = (
                    f"PR #{ci_pr_number} behind base, update_branch failed {_ub_failures}x "
                    f"— fix spawn suppressed (iteration cap)"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} behind base escalation — fix spawn suppressed ({source})"
                )
            return False

        # Real merge conflict ('dirty' state, or 409 conflict from the merge call).
        # Cannot be resolved by retrying — spawn a fix task with rebase instructions,
        # mirroring the CI-failure spawn path below.
        if merge_result.get("conflict") or _ms == "dirty":
            from app.review_pipeline import _spawn_pipeline_task
            from app.rollup_state import _is_rollup_parent_awaiting_merge

            # Dedup guard: if a fix task for this PR is already active, don't spawn another.
            # Mirrors the CI-failure path in handle_ci_completion to prevent duplicate fixes
            # contending on the same PR.
            # Phase F3 (H5): push pr_number filter into SQL via cast(..., Integer)
            # so ``ix_task_payloads_pr_number`` (alembic 0057) is usable on
            # Postgres. Same dialect-symmetric approach as review_pipeline.py.
            # status_log included to satisfy the Task event listeners under
            # async sessions; plan_output dropped via load_only.
            active_tasks = (
                (
                    await session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.context,
                                Task.payloads,
                                Task.project_id,
                                Task.status_log,
                            )
                        )
                        .where(
                            Task.project_id == task.project_id,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                ]
                            ),
                            Task.archived.is_(False),
                            _pr_number_match(session, ci_pr_number),
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_fix = next(
                (
                    t
                    for t in active_tasks
                    if t.id != task.id and t.first_payload.get("type") == "fix"
                ),
                None,
            )
            if existing_fix:
                if not _is_rollup_parent_awaiting_merge(task.context):
                    task.status = TaskStatus.COMPLETED
                task.latest_status_message = f"PR #{ci_pr_number} merge conflict, fix already active → {existing_fix.id}"
                logger.warning(
                    f"[CI] PR #{ci_pr_number} merge conflict, fix already exists → {existing_fix.id} ({source})"
                )
                return False
            conflict_prompt = (
                f"PR #{ci_pr_number} cannot merge into `{ci_base}` due to a merge conflict "
                f"(GitHub mergeable_state='dirty'). The code review is already approved — "
                f"focus only on resolving the conflict.\n\n"
                f"After cloning:\n"
                f"  1. Run `git fetch origin {ci_base} && git rebase origin/{ci_base}`.\n"
                f"  2. Resolve every conflict marker — preserve the intent of both branches.\n"
                f"  3. Re-run lint/type/test suites for the affected files.\n"
                f"  4. Commit the resolution and force-push with `--force-with-lease`.\n\n"
                f"Original merge error: {merge_msg[:500]}"
            )
            # Defense-in-depth (incident 2026-05-26..28): prefer the PR's
            # actual head ref over our parent-task-derived ``ci_branch`` when
            # they diverge.  The agent always pushes to ``blueprint/...`` but
            # the orchestrator derives from per-project namespace, so a
            # misconfigured ``task_branch_namespace`` strands the fix-task's
            # initial ``git clone --branch`` against a nonexistent ref.  The
            # PR is the source of truth for the branch we're trying to fix.
            _pr_head_ref = (_recheck_pr or {}).get("head", {}).get("ref", "")
            _spawn_branch = ci_branch
            if _pr_head_ref and ci_branch and _pr_head_ref != ci_branch:
                logger.warning(
                    "[CI] PR #%s branch drift: derived=%r vs PR.head.ref=%r — "
                    "using PR head ref for fix-spawn to avoid clone-against-missing-branch",
                    ci_pr_number,
                    ci_branch,
                    _pr_head_ref,
                )
                _spawn_branch = _pr_head_ref
            fix_task, _ = await _spawn_pipeline_task(
                task,
                "fix",
                session,
                ci_pr_number,
                _spawn_branch,
                ci_base,
                fix_prompt=conflict_prompt,
                original_task_id=ci_orig_id,
                fix_reason="merge_conflict",
                # Phase 2d: post-APPROVE — chain has ended at review approval,
                # the rebase fix must cold-provision (release-on-APPROVE).
                suppress_warm_handoff=True,
            )
            if fix_task:
                ci_loop_ctx = dict(ci_loop)
                ci_loop_ctx["ci_waiting"] = False
                ci_loop_ctx["ci_fix_spawned"] = str(fix_task.id)
                task.context = {**(task.context or {}), "review_loop": ci_loop_ctx}
                flag_modified(task, "context")
                if not _is_rollup_parent_awaiting_merge(task.context):
                    task.status = TaskStatus.COMPLETED
                task.latest_status_message = (
                    f"PR #{ci_pr_number} merge conflict — spawned fix → {fix_task.id}"
                )
                logger.warning(
                    f"[CI] PR #{ci_pr_number} merge conflict — spawned fix → {fix_task.id} ({source})"
                )
            else:
                task.latest_status_message = f"PR #{ci_pr_number} merge conflict — fix spawn suppressed (iteration cap)"
                logger.warning(
                    f"[CI] PR #{ci_pr_number} merge conflict — fix spawn suppressed ({source})"
                )
            return False

        if _is_rule_block:
            from app.shared_state import runtime_config as _rt_cfg

            # Discretionary gate: only approval-gate when require_approval_gates is on.
            # With gates off the status message + alert below still surface the block
            # (visibility preserved), but we do NOT set requires_approval — otherwise
            # services.py's 2h approval-gate timeout FAILs the CI task and, via the
            # orig escalation, strands the parent. The merge stays rule-blocked; the
            # normal backstop applies. (Mirrors the rollup review-cap/respawn-cap gating.)
            _gates_on = _rt_cfg.get("require_approval_gates", True)
            task.latest_status_message = (
                f"CI passed but merge blocked on PR #{ci_pr_number}: "
                f"{merge_msg[:200]} — requires manual intervention"
            )
            if _gates_on:
                task.requires_approval = True
            ci_loop["merge_rule_blocked"] = True
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            session.add(task)
            if ci_orig_id and _gates_on:
                try:
                    orig = await session.get(Task, uuid.UUID(ci_orig_id))
                    if orig:
                        orig.requires_approval = True
                        session.add(orig)
                except Exception as e:
                    logger.warning(
                        f"[CI] Failed to set requires_approval on {ci_orig_id}: {e}",
                        exc_info=True,
                    )
            await _maybe_create_failure_alert(
                task,
                session,
                f"PR #{ci_pr_number} merge blocked by repository rules — needs manual action",
            )
            logger.warning(
                f"[CI] PR #{ci_pr_number} merge blocked by repo rules — escalated ({source})"
            )
        else:
            _merge_failures = ci_loop.get("merge_failures", 0) + 1
            ci_loop["merge_failures"] = _merge_failures
            ci_loop["ci_poll_interval"] = min(30 * _merge_failures, 120)
            ci_loop["ci_last_polled"] = _utcnow().isoformat()
            # Track repeated-identical-error streak for early exit: if the merge
            # API keeps returning the same error, retrying won't change anything.
            _prev_err = ci_loop.get("merge_last_error")
            _norm_cur = _normalize_merge_error(merge_msg)
            _norm_prev = (
                _normalize_merge_error(_prev_err) if _prev_err is not None else None
            )
            if _norm_prev is not None and _norm_prev == _norm_cur:
                _err_streak = ci_loop.get("merge_error_streak", 1) + 1
            else:
                _err_streak = 1
            ci_loop["merge_last_error"] = merge_msg
            ci_loop["merge_error_streak"] = _err_streak
            task.context = {**(task.context or {}), "review_loop": ci_loop}
            task.latest_status_message = f"Merge attempt {_merge_failures} failed on PR #{ci_pr_number}: {merge_msg} — retrying"
            logger.warning(
                f"[CI] PR #{ci_pr_number} merge failed ({_merge_failures}x): {merge_msg} — will retry ({source})"
            )

            # Cross-PR systemic merge-blocker detection. If ≥N distinct PRs in
            # this project hit the same normalized error within the window,
            # pause retries (they won't succeed until the shared cause clears)
            # and raise a single alert surfacing the shared pattern. Resumes
            # automatically once the window ages out stale occurrences.
            from app.shared_state import (
                record_merge_error_occurrence,
                MERGE_PATTERN_MIN_PRS,
            )

            _project_key = str(task.project_id) if task.project_id else ""
            _pattern_prs = (
                record_merge_error_occurrence(_project_key, _norm_cur, ci_pr_number)
                if (_project_key and _norm_cur and ci_pr_number is not None)
                else 0
            )
            if _pattern_prs >= MERGE_PATTERN_MIN_PRS:
                ci_loop["merge_pattern_paused"] = True
                ci_loop["merge_pattern_pr_count"] = _pattern_prs
                ci_loop["ci_poll_interval"] = 120
                _already_alerted = ci_loop.get("merge_pattern_alerted")
                ci_loop["merge_pattern_alerted"] = True
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                flag_modified(task, "context")
                task.latest_status_message = (
                    f"PR #{ci_pr_number} merge paused — systemic pattern: "
                    f"{_pattern_prs} PRs blocked by same error "
                    f"({merge_msg[:100]}) — retries deferred"
                )
                if not _already_alerted:
                    await _maybe_create_failure_alert(
                        task,
                        session,
                        f"Systemic merge blocker in project: {_pattern_prs} PRs "
                        f"failing with the same error — check branch protection "
                        f"rules and CI environment. Error: {merge_msg[:200]}",
                    )
                logger.warning(
                    f"[CI] Cross-PR merge pattern detected — pausing retries "
                    f"for PR #{ci_pr_number} ({_pattern_prs} PRs affected) ({source})"
                )
                return False

            _hard_cap = _merge_hard_cap_for_task(task)
            _escalate_at = max(1, _hard_cap - 5)
            _final_warn_at = max(1, _hard_cap - 2)
            _deterministic = _is_deterministic_merge_error(_norm_cur)
            _repeated_identical = _err_streak >= 2

            # Spawn a rebase fix the first time a persistent merge failure appears
            # (internal 5/15/45s backoff above has already absorbed the stale-flag
            # window, so a failure that makes it here is either a real moving-parent
            # conflict or a true block).  Only one fix active per PR at a time;
            # the active-fix check is the gate — no counter, no sticky flag, so
            # if the fix completes and the next merge still fails, a fresh fix is
            # spawned.  Rule blocks are handled in the _is_rule_block branch above
            # and never reach here.  Skip rebase-fix spawn when the early-exit
            # conditions below would FAIL the task anyway (deterministic error,
            # repeated-identical streak, or hard-cap reached) — a rebase can't
            # recover from those and the test suite verifies those paths exit
            # cleanly without spawning.  Also skip when GitHub reports the PR as
            # 'clean' — the merge failure is a transient API hiccup, not a real
            # conflict; spawning a rebase fix against a clean PR is always wrong.
            if not (
                _deterministic
                or _repeated_identical
                or _merge_failures >= _hard_cap
                or _ms == "clean"
            ):
                from app.review_pipeline import _spawn_pipeline_task

                # Phase F3 (H5): push pr_number filter into SQL via cast(..., Integer)
                # so ``ix_task_payloads_pr_number`` (alembic 0057) is usable on
                # Postgres. status_log included for the Task event listeners
                # under async sessions; plan_output dropped via load_only.
                active_tasks = (
                    (
                        await session.execute(
                            select(Task)
                            .options(
                                load_only(
                                    Task.id,
                                    Task.status,
                                    Task.context,
                                    Task.payloads,
                                    Task.project_id,
                                    Task.status_log,
                                )
                            )
                            .where(
                                Task.project_id == task.project_id,
                                Task.status.in_(
                                    [
                                        TaskStatus.QUEUED,
                                        TaskStatus.PROVISIONING,
                                        TaskStatus.IN_PROGRESS,
                                        TaskStatus.EVALUATING,
                                    ]
                                ),
                                Task.archived.is_(False),
                                _pr_number_match(session, ci_pr_number),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                already_fixing = any(
                    t.id != task.id and t.first_payload.get("type") == "fix"
                    for t in active_tasks
                )
                if not already_fixing:
                    rebase_prompt = (
                        f"PR #{ci_pr_number} was approved and CI passed, but GitHub rejected the merge: "
                        f"{merge_msg[:300]}\n\n"
                        f"The base branch ({ci_base}) likely advanced since this PR's last rebase "
                        f"(sibling PRs merging into the same parent).  Rebase onto the latest "
                        f"{ci_base} so the PR moves forward:\n\n"
                        f"1. git fetch origin {ci_base}\n"
                        f"2. git rebase origin/{ci_base}\n"
                        f"3. If no conflicts and no commits were replayed, the PR is already "
                        f"up-to-date — GitHub's mergeability flag is just stale.  Report no "
                        f"changes needed (don't force-push); retries will clear it.\n"
                        f"4. Otherwise resolve conflicts and git push --force-with-lease.\n"
                    )
                    fix_task, _ = await _spawn_pipeline_task(
                        task,
                        "fix",
                        session,
                        ci_pr_number,
                        ci_branch,
                        ci_base,
                        fix_prompt=rebase_prompt,
                        original_task_id=ci_orig_id,
                        # Phase 2d: post-APPROVE — review approved + CI passed
                        # but merge rejected. Rebase fix must cold-provision
                        # (release-on-APPROVE).
                        suppress_warm_handoff=True,
                    )
                    if fix_task:
                        ci_loop["ci_waiting"] = (
                            False  # hand off; fix-completion will re-park
                        )
                        task.context = {**(task.context or {}), "review_loop": ci_loop}
                        from app.rollup_state import _is_rollup_parent_awaiting_merge

                        if not _is_rollup_parent_awaiting_merge(task.context):
                            task.status = TaskStatus.COMPLETED
                        task.latest_status_message = f"Merge blocked on PR #{ci_pr_number} ({merge_msg[:100]}) — spawned rebase fix → {fix_task.id}"
                        logger.info(
                            f"[CI] PR #{ci_pr_number} merge blocked, spawned rebase fix → {fix_task.id} ({source})"
                        )
                        session.add(task)
                        await session.commit()
                        return True

            if _merge_failures >= _escalate_at and not ci_loop.get("merge_escalated"):
                ci_loop["merge_escalated"] = True
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                await _maybe_create_failure_alert(
                    task,
                    session,
                    f"Rollup PR #{ci_pr_number} merge failed {_merge_failures} times — still retrying",
                )
            if (
                _merge_failures >= _final_warn_at
                and ci_loop.get("merge_escalated")
                and not ci_loop.get("merge_final_warn")
            ):
                ci_loop["merge_final_warn"] = True
                task.context = {**(task.context or {}), "review_loop": ci_loop}
                await _maybe_create_failure_alert(
                    task,
                    session,
                    f"PR #{ci_pr_number} merge failed {_merge_failures} times — "
                    f"hard cap imminent ({_hard_cap - _merge_failures} retries remaining)",
                )
            if _merge_failures >= _hard_cap or _repeated_identical or _deterministic:
                from app.shared_state import runtime_config as _rt_cfg

                # Gate only the approval flag — FAILED is the terminalization and stays.
                # With gates off the task still FAILs (with its status message + alert),
                # but does not approval-gate the orig/parent (which services.py would
                # otherwise 2h-timeout-FAIL). Mirrors the rollup review-cap gating.
                _gates_on = _rt_cfg.get("require_approval_gates", True)
                task.status = TaskStatus.FAILED
                if _gates_on:
                    task.requires_approval = True
                _early_exit = (
                    _repeated_identical or _deterministic
                ) and _merge_failures < _hard_cap
                if _early_exit:
                    _retries_saved = _hard_cap - _merge_failures
                    task.context = {
                        **(task.context or {}),
                        "_merge_retries_saved": _retries_saved,
                        "review_loop": ci_loop,
                    }
                    if _deterministic and not _repeated_identical:
                        task.latest_status_message = (
                            f"PR #{ci_pr_number} merge failed with deterministic "
                            f"non-retryable error: {merge_msg[:200]} — "
                            f"giving up, needs manual intervention"
                        )
                    else:
                        task.latest_status_message = (
                            f"PR #{ci_pr_number} merge failed with identical error "
                            f"{_err_streak} times in a row: {merge_msg[:200]} — "
                            f"giving up, needs manual intervention"
                        )
                else:
                    task.latest_status_message = (
                        f"PR #{ci_pr_number} merge failed {_merge_failures} times "
                        f"(>= {_hard_cap}) — giving up, needs manual intervention"
                    )
                if ci_orig_id and _gates_on:
                    try:
                        orig = await session.get(Task, uuid.UUID(ci_orig_id))
                        if orig:
                            orig.requires_approval = True
                            session.add(orig)
                    except Exception as e:
                        logger.warning(
                            f"[CI] Failed to set requires_approval on {ci_orig_id}: {e}",
                            exc_info=True,
                        )
                await _maybe_create_failure_alert(
                    task,
                    session,
                    f"PR #{ci_pr_number} merge hard-capped after {_merge_failures} failures — manual action required",
                )
                logger.error(
                    f"[CI] PR #{ci_pr_number} hit merge hard cap ({_merge_failures}) — marking FAILED ({source})"
                )
                from app.failure_classification import (
                    classify_failure,
                    persist_failure_category,
                )
                from app.routers.agent_status import record_task_result
                from app.shared_state import record_direction_result

                _fc = classify_failure(task)
                persist_failure_category(task, _fc)
                record_task_result(
                    success=False,
                    failure_class=_fc,
                    task_id=str(task.id),
                    task_type=task.first_payload.get("type", ""),
                    task_context=dict(task.context or {}),
                    auto_remediation=task.auto_remediation,
                )
                _direction_id = str((task.context or {}).get("direction_id") or "")
                if _direction_id:
                    record_direction_result(
                        _direction_id,
                        success=False,
                        failure_class=_fc,
                        project_id=str(task.project_id) if task.project_id else "",
                    )
                if _early_exit and task.project_id:
                    from app.broadcast import broadcast_to_project

                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {
                            "task_id": str(task.id),
                            # task.status was set to FAILED at the top of this
                            # hard-cap branch; use the literal so a possibly-expired
                            # instance (failure-alert DB op above) isn't lazy-loaded.
                            "status": TaskStatus.FAILED,
                            "details": {
                                "merge_early_exit": True,
                                "retries_saved": _retries_saved,
                                "error_streak": _err_streak,
                            },
                        },
                    )
    except Exception:
        ci_loop["ci_poll_interval"] = 30
        ci_loop["ci_last_polled"] = _utcnow().isoformat()
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        task.latest_status_message = (
            f"Merge check failed on PR #{ci_pr_number} — retrying"
        )
        logger.warning(
            f"[CI] PR #{ci_pr_number} merge recheck failed — will retry ({source})"
        )
    return False


async def handle_ci_completion(
    task: Task,
    ci_result: dict,
    gh_token: str,
    owner: str,
    repo: str,
    session: AsyncSession,
    *,
    source: str = "poll",
) -> bool:
    """Process CI completion for a ci_waiting task.

    Args:
        task: The EVALUATING task with ci_waiting=True
        ci_result: Result from get_check_runs (status, conclusion, failed_logs, details)
        gh_token: GitHub token for API calls
        owner, repo: GitHub repo coordinates
        session: DB session
        source: "webhook" or "poll" for logging

    Returns:
        True if an action was taken (merge, fix spawn, etc.), False if CI still pending.
    """
    from app.review_pipeline import _spawn_pipeline_task
    from app.github import create_pr_comment
    from app.main import _maybe_create_failure_alert
    from app.broadcast import broadcast_to_project
    from app.models import Project
    from app.shared_state import get_project_integration_branch

    # Serialize concurrent callers (webhook + poller) by locking the task row.
    # skip_locked: if another handler already holds the lock, exit — it will
    # handle the CI result.
    locked_task = (
        (
            await session.execute(
                select(Task).where(Task.id == task.id).with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if locked_task is None:
        logger.info(
            f"[CI] Skipping CI completion for task {task.id} — lock held by concurrent handler ({source})"
        )
        return False
    task = locked_task

    # Identity snapshot: a rollback inside the failure-class helpers further
    # below can expire ``task`` (a rollback defers every attr even with
    # expire_on_commit=False), so the final broadcast must not re-read it.
    # id/project_id are immutable — capture them now while ``task`` is fresh.
    _bcast_task_id = str(task.id)
    _bcast_project_id = str(task.project_id) if task.project_id else ""

    # Re-check: task may have been completed by the concurrent handler that held the lock
    if task.status != TaskStatus.EVALUATING:
        logger.info(
            f"[CI] Task {task.id} no longer EVALUATING (now {task.status}) — skipping ({source})"
        )
        return False

    ci_loop = (task.context or {}).get("review_loop", {})
    ci_pr_number = ci_loop.get("pr_number")
    ci_payload = task.first_payload
    ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
    _ci_project = (
        await session.get(Project, task.project_id) if task.project_id else None
    )
    ci_base = (
        ci_loop.get("base_branch", "")
        or ci_payload.get("base_branch", "")
        or get_project_integration_branch(_ci_project)
    )
    ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get(
        "original_task_id", ""
    )

    if ci_result["status"] != "completed":
        return False

    if ci_result["conclusion"] == "success":
        # Record the CI gate signal. JSONB columns don't track in-place dict
        # mutations — flag_modified ensures the change is flushed to disk.
        ci_loop["ci_passed"] = True
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")

        if not _review_approved(ci_loop):
            # Review hasn't approved yet — leave parked, just persist the signal.
            # The review task's APPROVE handler will write review_approved on the
            # parent and trigger the gate when it runs.
            task.latest_status_message = (
                f"CI passed on PR #{ci_pr_number} — waiting for review approval"
            )
            logger.info(
                f"[CI] PR #{ci_pr_number} CI passed, waiting for review approval ({source})"
            )
            session.add(task)
            _bcast_status = task.status
            await session.commit()
            await broadcast_to_project(
                _bcast_project_id,
                "task_updated",
                {"task_id": _bcast_task_id, "status": _bcast_status},
            )
            return True

        # Both gates met — perform the merge
        await _attempt_merge_with_retry_and_escalation(
            task, gh_token, owner, repo, session, source=source
        )
    else:
        # CI failed — spawn fix task
        # Phase F3 (H5): push pr_number filter into SQL via cast(..., Integer)
        # so ``ix_task_payloads_pr_number`` (alembic 0057) is usable on
        # Postgres. status_log included for the Task event listeners under
        # async sessions; plan_output dropped via load_only.
        active_tasks = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.context,
                            Task.payloads,
                            Task.project_id,
                            Task.status_log,
                        )
                    )
                    .where(
                        Task.project_id == task.project_id,
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                            ]
                        ),
                        Task.archived.is_(False),
                        _pr_number_match(session, ci_pr_number),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_fix = next(
            (
                t
                for t in active_tasks
                if t.id != task.id and t.first_payload.get("type") == "fix"
            ),
            None,
        )
        if existing_fix:
            from app.rollup_state import _is_rollup_parent_awaiting_merge

            # Rollup parents stay in EVALUATING — ci_completion will promote them
            # via pr_merged=True once the fix lands and the PR merges.
            if not _is_rollup_parent_awaiting_merge(task.context):
                task.status = TaskStatus.COMPLETED
            task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix already active → {existing_fix.id}"
            logger.error(
                f"[CI] PR #{ci_pr_number} CI failed, fix already exists → {existing_fix.id} ({source})"
            )
            session.add(task)
            _bcast_status = task.status
            await session.commit()
            await broadcast_to_project(
                _bcast_project_id,
                "task_updated",
                {"task_id": _bcast_task_id, "status": _bcast_status},
            )
            return True

        ci_errors = ci_result.get("failed_logs", "CI checks failed")

        # ── C2 lock-window break (CI-failure path) ──
        # The task row has been held FOR UPDATE since ~1341. ``create_pr_comment``
        # is a GitHub POST that can block 5-30s under rate-limit; holding the
        # task lock across it starves concurrent heartbeats on this row and can
        # trip the 60s idle_in_transaction timeout. Mirror the merge SUCCESS
        # path (``_attempt_merge_with_retry_and_escalation``): capture a content
        # snapshot, commit-release the lock, post the comment lock-free, then
        # re-acquire with snapshot validation. The subsequent
        # ``_spawn_pipeline_task`` manages its own per-PR advisory lock and
        # commits internally, so it does not need the task lock held — but the
        # terminal status writes below DO, hence the relock. If a peer mutated
        # the gate fields during the unlocked window we defer to the next sweep.
        _ci_fail_task_id = task.id
        _ci_fail_snapshot = _capture_merge_snapshot(task)
        flag_modified(task, "context")
        session.add(task)
        await session.commit()  # release the task FOR UPDATE lock

        await create_pr_comment(
            gh_token,
            owner,
            repo,
            ci_pr_number,
            f"**Blueprint CI**: Checks failed.\n\n```\n{ci_errors[:2000]}\n```",
        )

        relocked = await _relock_merge_task(
            session,
            _ci_fail_task_id,
            _ci_fail_snapshot,
            task,
            source=source,
        )
        if relocked is None:
            logger.info(
                f"[CI] Task {_bcast_task_id} held/mutated during CI-failure "
                f"comment window — deferring fix spawn to next sweep ({source})"
            )
            return False
        task = relocked

        from app.review_pipeline import build_ci_failure_fix_prompt

        ci_fix_prompt = build_ci_failure_fix_prompt(
            pr_number=ci_pr_number,
            base_branch=ci_base,
            ci_errors=ci_errors,
            owner=owner,
            repo=repo,
        )
        fix_task, _ = await _spawn_pipeline_task(
            task,
            "fix",
            session,
            ci_pr_number,
            ci_branch,
            ci_base,
            fix_prompt=ci_fix_prompt,
            original_task_id=ci_orig_id,
            fix_reason="ci_failure",
            ci_failure_logs=ci_errors,
            # Phase 2d: CI ran as a merge gate after review approval —
            # the warm chain has ended, the new fix must cold-provision.
            suppress_warm_handoff=True,
        )
        if fix_task:
            from app.rollup_state import _is_rollup_parent_awaiting_merge

            ci_loop_ctx = dict(ci_loop)
            ci_loop_ctx["ci_waiting"] = False
            ci_loop_ctx["ci_fix_spawned"] = str(fix_task.id)
            task.context = {**(task.context or {}), "review_loop": ci_loop_ctx}
            # Rollup parents stay in EVALUATING — ci_completion will promote them
            # via pr_merged=True once the fix lands and the PR merges.
            if not _is_rollup_parent_awaiting_merge(task.context):
                task.status = TaskStatus.COMPLETED
            task.latest_status_message = (
                f"CI failed on PR #{ci_pr_number}, spawned fix → {fix_task.id}"
            )
            logger.error(
                f"[CI] PR #{ci_pr_number} CI failed, spawned fix → {fix_task.id} ({source})"
            )
        else:
            _ci_fail_msg = (
                f"CI failed on PR #{ci_pr_number}, fix not spawned (pipeline limit)"
            )
            task.status = TaskStatus.FAILED
            task.latest_status_message = _ci_fail_msg
            logger.error(
                f"[CI] PR #{ci_pr_number} CI failed, fix spawn returned None ({source})"
            )
            # Persist the terminal FAILED transition NOW, on a known-healthy
            # session, before the best-effort failure accounting below. Of the
            # accounting helpers only ``_maybe_create_failure_alert`` touches
            # the DB; an inner error there poisons the transaction, so a
            # deferred commit would raise PendingRollbackError and strand the
            # task in EVALUATING. Committing first makes FAILED durable.
            session.add(task)
            await session.commit()
            await broadcast_to_project(
                _bcast_project_id,
                "task_updated",
                {"task_id": _bcast_task_id, "status": TaskStatus.FAILED},
            )

            # Best-effort failure accounting (classification, deploy-monitor
            # signal, direction streak, alert). Isolated so a DB error here
            # cannot undo the FAILED status already committed above.
            try:
                from app.failure_classification import (
                    classify_failure,
                    persist_failure_category,
                )
                from app.routers.agent_status import record_task_result
                from app.shared_state import record_direction_result

                _fc = classify_failure(task)
                persist_failure_category(task, _fc)
                record_task_result(
                    success=False,
                    failure_class=_fc,
                    task_id=_bcast_task_id,
                    task_type=task.first_payload.get("type", ""),
                )
                _direction_id = str((task.context or {}).get("direction_id") or "")
                if _direction_id:
                    record_direction_result(
                        _direction_id,
                        success=False,
                        failure_class=_fc,
                        project_id=_bcast_project_id,
                    )
                await _maybe_create_failure_alert(task, session, _ci_fail_msg)
                session.add(task)
                await session.commit()
            except Exception as acct_exc:
                logger.warning(
                    "[CI] failure accounting errored for task %s (%s) — FAILED "
                    "status already persisted",
                    _bcast_task_id,
                    acct_exc,
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
            return True

    # Reached only by the fix-spawned branch above — the fix-not-spawned
    # branch commits and returns inline. ``task`` CAN be expired here: the
    # nested _spawn_pipeline_task / prior session work may have rolled back,
    # and rollback expires instances even with expire_on_commit=False.
    session.add(task)
    # Snapshot status before commit. A rollback earlier on this path (e.g. inside
    # _spawn_pipeline_task) expires `task` even with expire_on_commit=False, so a
    # post-commit task.status read here does a sync lazy-load in async context →
    # MissingGreenlet (same family as the #6754 claim_task fix).
    _bcast_status = task.status
    await session.commit()
    await broadcast_to_project(
        _bcast_project_id,
        "task_updated",
        {"task_id": _bcast_task_id, "status": _bcast_status},
    )
    return True
