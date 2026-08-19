"""Merge-helper layer extracted from ci_completion.py.

Houses the IO-free merge-decision policy (scope-aware retry-cap resolution),
the merge-error normalization / classification helpers, the one-time merge
summary comment, and the lock-window snapshot/relock idiom. ci_completion.py
re-exports every public name here via a top-of-file ``# noqa: F401`` block so
existing ``from app.ci_completion import ...`` call sites (and tests) keep
resolving unchanged.

This module must NOT import ci_completion (keep the dependency acyclic) — the
big merge-retry/escalation routine and the ``handle_ci_completion`` entrypoint
stay in ci_completion.py and import these helpers, not the other way around.
"""

import logging
import os
import re

from sqlalchemy import func
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import Task
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
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        return func.json_extract(Task.payloads, "$[0].pr_number") == pr_number
    return Task.payloads[0]["pr_number"].as_string() == str(pr_number)


# Hard cap on merge retries — scope-aware. Small-scope PRs (single-file tweaks)
# have fewer legitimate reasons for transient merge failures, so cap sooner.
MERGE_FAILURE_HARD_CAP = 15
_SCOPE_MERGE_HARD_CAP = {"small": 8, "medium": 12, "large": 15}

# Single escalation rung for the per-PR stuck-merge ladder: how many failures
# before the hard cap the one (deduped) "still stuck, retrying" operator alert
# fires. Past this point the next milestone is the terminal hard-cap FAIL — no
# intermediate "final-warn" rung (that second warning was redundant: the same
# stuck PR already alerted once here and the operator gains nothing from a
# countdown). Tunable via env to move the warning closer to / further from the
# cap without touching the cap itself.
MERGE_ESCALATE_BEFORE_CAP = int(os.getenv("MERGE_ESCALATE_BEFORE_CAP", "5"))


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


def _review_not_required(review_loop: dict) -> bool:
    """Return whether a recognized no-initial-review policy marker applies.

    The marker authorizes only a chain that has never accumulated review
    history and carries the backend-written policy reason. If history exists
    (or is malformed), the chain has entered the authoritative review protocol
    and a stale marker cannot bypass it.
    """
    history = review_loop.get("review_history")
    history_is_empty = history is None or (
        isinstance(history, list) and len(history) == 0
    )
    policy_reason = review_loop.get("review_policy_reason")
    pristine_review_state = bool(
        review_loop.get("issue_ledger") is None
        and not any(
            review_loop.get(key)
            for key in (
                "authoritative_review_required",
                "authoritative_review_task_id",
                "authoritative_review_target",
                "review_wait_started",
                "review_approved",
            )
        )
    )
    return bool(
        review_loop.get("review_not_required") is True
        and history_is_empty
        and pristine_review_state
        and isinstance(policy_reason, str)
        and policy_reason.strip()
    )


async def _fetch_live_review_target(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    project_id: str,
) -> dict[str, object]:
    """Fetch the immutable GitHub merge identity used by review authority."""
    from app.github import get_pull_request

    pr_data = await get_pull_request(
        gh_token,
        owner,
        repo,
        pr_number,
        project_id=project_id or None,
    )
    raw_merged = pr_data.get("merged")
    return {
        "project_id": project_id,
        "pr_number": int(pr_number),
        "base_sha": str((pr_data.get("base") or {}).get("sha") or ""),
        "head_sha": str((pr_data.get("head") or {}).get("sha") or ""),
        "head_ref": str((pr_data.get("head") or {}).get("ref") or ""),
        "base_ref": str((pr_data.get("base") or {}).get("ref") or ""),
        "state": str(pr_data.get("state") or ""),
        # Preserve GitHub's boolean proof as a tri-state. Missing or malformed
        # data is unknown, not objective evidence that the PR is unmerged.
        "merged": raw_merged if isinstance(raw_merged, bool) else None,
        "draft": pr_data.get("draft") is True,
        "mergeable": pr_data.get("mergeable"),
        "mergeable_state": str(pr_data.get("mergeable_state") or ""),
        "merge_commit_sha": str(pr_data.get("merge_commit_sha") or ""),
    }


async def _merge_commit_parent_matches_checked_base(
    gh_token: str,
    owner: str,
    repo: str,
    merge_commit_sha: str,
    checked_base_sha: str,
    *,
    project_id: str,
) -> bool:
    """Prove a squash merge was created from the intent's checked base.

    A merged PR's live ``base.sha`` is ambient and can advance after landing.
    The immutable squash commit's first parent is the stable pre-merge base
    identity needed to reconcile a response-lost PUT without weakening the
    exact base+head intent.
    """

    if not merge_commit_sha or not checked_base_sha:
        return False
    from app.github import _github_request

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/commits/{merge_commit_sha}",
        headers,
        project_id=project_id,
        consumer="approval_merge_reconciliation",
    )
    if response.status_code != 200:
        raise RuntimeError(
            "merge commit parent proof unavailable: "
            f"GitHub returned HTTP {response.status_code}"
        )
    parents = response.json().get("parents") or []
    return bool(
        parents and str((parents[0] or {}).get("sha") or "") == str(checked_base_sha)
    )


def _live_target_is_merge_submission_safe(
    target: dict[str, object], *, base_branch: str
) -> bool:
    """Reject GitHub states that prove the checked target is not landable.

    GitHub does not expose an atomic compare-and-swap for the base SHA. The
    same-base advisory lock closes Blueprint-vs-Blueprint movement; external
    movement still relies on branch protection/up-to-date checks. This helper
    fails closed on the concrete stale/closed/conflicted states visible in the
    final GET without pretending an ``unknown`` mergeability value is a CAS.
    """
    state = str(target.get("state") or "").lower()
    mergeable_state = str(target.get("mergeable_state") or "").lower()
    base_ref = str(target.get("base_ref") or "")
    return not (
        target.get("merged") is True
        or target.get("draft") is True
        or target.get("mergeable") is False
        or (state and state != "open")
        or mergeable_state in {"behind", "dirty", "draft"}
        or (base_ref and base_ref != base_branch)
    )


def _review_approved(review_loop: dict) -> bool:
    """Return whether the durable review policy permits the merge path.

    A reviewed chain must carry the explicit ``review_approved=True`` flag.
    A policy path that deliberately skipped review must instead carry the
    explicit ``review_not_required=True`` marker.  The old free-standing
    ``verdict == "APPROVE"`` value is not merge authority: it has neither an
    authoritative blocker ledger nor immutable target provenance.
    """
    return review_loop.get("review_approved") is True or _review_not_required(
        review_loop
    )


def revoke_review_authority_for_active_fix(
    task: Task,
    fix_task_id=None,
    *,
    review_loop: dict | None = None,
    context_snapshot: dict | None = None,
) -> dict:
    """Make a code-writing fix a durable merge-authority boundary.

    A fix can change the PR head, so neither an older exact-head APPROVE nor an
    initial-review policy exemption may survive while that fix is active.  The
    history, ledger, and prior target remain available for cheap delta review;
    only authority/transient merge-gate state is cleared.  Callers must commit
    this mutation no later than the transaction that makes ``fix_task_id``
    active.
    """
    context = dict(
        context_snapshot if context_snapshot is not None else (task.context or {})
    )
    loop = dict(
        review_loop if review_loop is not None else (context.get("review_loop") or {})
    )
    loop["review_approved"] = False
    loop["ci_passed"] = False
    loop["ci_waiting"] = False
    if fix_task_id is not None:
        loop["ci_fix_spawned"] = str(fix_task_id)
    else:
        loop.pop("ci_fix_spawned", None)
    for stale_key in (
        "verdict",
        "review_not_required",
        "review_policy_reason",
        "authoritative_review_required",
        "authoritative_review_task_id",
        "authoritative_review_target",
        "authoritative_review_target_unresolved_at",
    ):
        loop.pop(stale_key, None)
    task.context = {**context, "review_loop": loop}
    flag_modified(task, "context")
    return loop


def effective_pr_number(task_or_ctx) -> int | None:
    """Canonical PR-number accessor for a CI-waiting rollup task.

    Resolves ``review_loop.pr_number or context.rollup_pr_number`` — the SAME
    fallback the recovery sweeps already use (recovery_evaluating_pr_check.py,
    recovery_evaluating_approval.py, recovery_evaluating_strand.py, admin.py).

    Why this exists: the merge-critical reads (the orchestrator CI-poll loop and
    ``handle_ci_completion`` / ``_attempt_merge_with_retry_and_escalation``)
    historically read ONLY ``review_loop.pr_number``. A rollup parked with
    ``ci_waiting=True`` but its PR identity living only at
    ``context.rollup_pr_number`` (a re-arm path that never co-stamped
    ``review_loop.pr_number``) was therefore SKIPPED by the merger while the
    recovery sweeps still saw it — a PR-identity split-brain where the merger and
    the reaper disagree on whether a task even has a PR, so such rows only drain
    slowly out-of-band. Routing every merge-critical read through this accessor
    makes them agree with the recovery paths.

    Accepts a ``Task`` (reads ``.context``) or a bare context dict.
    """
    ctx = getattr(task_or_ctx, "context", None)
    if ctx is None and isinstance(task_or_ctx, dict):
        ctx = task_or_ctx
    ctx = ctx or {}
    review_loop = ctx.get("review_loop") or {}
    return review_loop.get("pr_number") or ctx.get("rollup_pr_number")


def arm_ci_wait(
    review_loop: dict,
    *,
    pr_number: int,
    head_sha: str | None = None,
    base_branch: str | None = None,
    now_iso: str,
    pause_baseline: float = 0.0,
) -> dict:
    """Arm a ``review_loop`` for the orchestrator's CI-wait / merge poll, in place.

    Every CI-wait arm site MUST co-stamp the PR identity and the wait-start
    clock together. Two failure shapes motivated this helper:

      * ``ci_waiting=True`` written WITHOUT ``pr_number`` is SKIPPED by the
        merge-critical poll loop (the PR-identity split-brain — see
        :func:`effective_pr_number`), so the rollup only drains out-of-band.
      * ``ci_waiting=True`` written WITHOUT ``ci_wait_started`` silently disables
        the CI-wait timeout (the poll loop carries a defensive backfill for
        exactly this — evidence some path armed without it).

    Routing arm sites through this helper guarantees both are present, so no arm
    site can reintroduce the identity-less ``{ci_waiting, iteration}`` shape.
    Returns the same dict for chaining.

    ``pr_number`` is asserted truthy: arming a CI-wait with no PR to poll is
    always a bug — resolve it (via :func:`effective_pr_number` or a local) first.
    """
    assert pr_number, "arm_ci_wait requires a resolved pr_number"
    review_loop["ci_waiting"] = True
    review_loop["pr_number"] = pr_number
    review_loop["ci_wait_started"] = now_iso
    review_loop["ci_wait_pause_baseline"] = pause_baseline
    if head_sha is not None:
        review_loop["head_sha"] = head_sha
    if base_branch is not None:
        review_loop["base_branch"] = base_branch
    return review_loop


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
      - review_loop.pending_reopen: C4 — set True by the orchestrator's CI
        poll when it self-closes the PR for a mergeability reset. Including
        it here means a poll-cycle commit of pending_reopen=True (i.e. an
        in-flight reset) DURING the merge's unlocked GitHub window
        invalidates the snapshot, so _relock_merge_task defers rather than
        reaching the closed-state branch on a PR we deliberately closed.
      - _rollup_lineage_generation: advanced whenever descendant work is
        requeued, invalidating review/CI proof captured for an older child set.
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
        "review_loop_pending_reopen": bool(review_loop.get("pending_reopen")),
        "rollup_lineage_generation": ctx.get("_rollup_lineage_generation", 0),
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


def locally_refused_merge_status(merge_result: dict, pr_number: int) -> str | None:
    """Operator-facing status for a merge that was refused BEFORE any PUT.

    ``merge_pull_request`` has two locally-refused outcomes that never reach
    GitHub's merge endpoint yet share the ``{"merged": False, "conflict": False}``
    shape of a pending-checks 405:

    * ``review_veto``          — GitHub's aggregate reviewDecision is
      CHANGES_REQUESTED, so the autonomous lane holds.
    * ``pat_fallback_blocked`` — the only credential the resolver could produce
      was the operator's own personal access token, and merging with it would
      mint a commit GitHub attributes to the operator for automated pipeline
      work.

    Both are terminal for the caller: there is nothing on the PR to re-check,
    no GitHub write may be issued on this credential (an ``update_branch``
    "catch-up" would mint exactly the operator-attributed commit the PAT
    refusal exists to prevent), and no merge-failure counter/escalation should
    fire — the PR itself is fine.  Returns the status message to surface, or
    ``None`` when the result is not a local refusal.
    """
    if merge_result.get("pat_fallback_blocked"):
        return (
            f"Merge not submitted for PR #{pr_number} — only an operator personal "
            "access token is available and the pipeline must not mint "
            "operator-attributed merge commits. Install or repair the GitHub App "
            "installation for this repository, then re-run the merge."
        )
    if merge_result.get("review_veto"):
        return (
            f"Merge held — GitHub has outstanding changes requested on PR #{pr_number}"
        )
    return None
