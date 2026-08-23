"""Binding issue-ledger and exact-review proof gate for PR publication."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.context_bounds import REVIEW_HISTORY_MAX_ENTRIES
from app.database import resolve_dialect_name
from app.issue_ledger import (
    build_issue_ledger,
    issue_ledger_complete_for_history,
)
from app.models import Task, TaskStatus, _utcnow
from app.review_loop_convergence import REVIEW_LOOP_ABANDON_CANDIDATE_KEY
from app.time_utils import _coerce_naive_utc

logger = logging.getLogger(__name__)

# The synthetic blocker raised when the durable ledger cannot prove it covers
# every review row.  Unlike every other entry in the open set it describes the
# bookkeeping, not the diff, so no commit fixes it and no reviewer refutes it.
LEDGER_PROOF_INCOMPLETE_ISSUE_ID = "BP-ledger-proof-incomplete"

# How many exact-target reviews this gate may spawn for ONE immutable
# ``(base_sha, head_sha)`` pair before it stops asking and hands the PR to an
# operator.
#
# Re-reviewing the same target is legitimate once or twice: the binding
# blocker set is accumulated across review rows, so a later reviewer can
# refute an earlier reviewer's finding and move it out of ``open``.  What is
# never legitimate is doing it without end.  With the head pinned, every spawn
# reads the identical diff; if this many independent adjudications of that
# diff have not cleared the ledger, the next one will not either, and the only
# thing still changing is the runner slot it occupies.
#
# The measured defect this bounds: PR #17693 took 441 exact-target review
# spawns over 9.5 hours -- one every ~66 seconds, continuously occupying one
# of three fleet runners -- and no cap fired.  The per-chain iteration cap
# counts actioned *generations*, and ``actioned_review_cap_tasks`` collapses
# every same-target review into a single generation, so ``chain_iterations``
# stayed at 1 forever.  That collapsing is correct for what the cap measures;
# it simply means the cap is not, and cannot be, the counter that bounds
# same-target respawn.  This is.
_EXACT_TARGET_REVIEW_SPAWN_BUDGET = 5

# Read-model only, all three.  They are re-derived from task rows on every gate
# pass by ``_census_exact_target_reviews`` and written back so an operator (and
# admin diagnostics) can read the count and the reasons behind it without
# re-running the census query.  Nothing reads them as authority.
#
# They used to BE the authority, and that is the defect this replaces.  The
# count lived in ``review_loop`` on whichever task the gate happened to be
# called with, and that task is not stable across the loop:
# ``orchestrator_ci_monitor`` drives *every* EVALUATING row whose
# ``review_loop.ci_waiting`` is truthy; when a spawned review APPROVEs but the
# merge is blocked, ``_process_post_commit`` parks the review task itself in
# EVALUATING + ``ci_waiting``, minting a NEW driver row for the same PR; and
# ``_spawn_pipeline_task`` rebuilds that row's ``review_loop`` from a fixed key
# allowlist that never carried these two keys.  Each round therefore handed the
# gate a fresh row reading ``0`` prior spawns, so the count could not climb past
# 1 and a budget of 5 never bound.  Deriving it from rows is immune to that
# migration: the rows are the same rows no matter which of them is driving.
_SPAWN_BUDGET_TARGET_KEY = "authoritative_review_spawn_target"
_SPAWN_BUDGET_COUNT_KEY = "authoritative_review_spawns"
_SPAWN_BUDGET_CENSUS_KEY = "authoritative_review_spawn_census"

# A review row in one of these statuses has already consumed its adjudication
# slot for the target it was spawned against: the agent reported.  EVALUATING
# is load-bearing here and not an oversight -- it is exactly the post-report
# park ``_process_post_commit`` uses for an approved-but-merge-blocked review,
# i.e. the migrated driver row itself, and the row that must be counted for the
# budget to bind at all.
#
# QUEUED / PROVISIONING / IN_PROGRESS have not reported yet, and BLOCKED /
# PARKED are holding states in which no diff was ever adjudicated.  While one
# of those five exists ``_spawn_pipeline_task`` can hand the gate back that
# same row rather than minting a new one, so charging them would park the PR
# against reviews that never reported.
#
# EVALUATING is in ``_PIPELINE_ACTIVE_STATUSES`` and holds its
# ``ux_task_active_idempotency_key`` slot exactly like those five do; what
# separates it here is the report, not the slot.  The key carries the
# iteration, which advances every round, so a post-report park never blocks
# the next spawn -- which is precisely how the runaway loop kept minting rows.
_REVIEW_ADJUDICATION_CONSUMED_STATUSES = (
    TaskStatus.EVALUATING,
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)

# Bound on the id list persisted with the census.  Enough to identify the
# offending rows from an alert without writing an unbounded list into a
# GIN-indexed JSONB column.
_CENSUS_TASK_ID_SAMPLE = 20


def _parse_reset_at(value: object) -> datetime | None:
    """Parse an ``iteration_reset_at`` stamp into naive UTC.

    ``Task.created_at`` is ``NaiveUTCDateTime`` and every comparison below is
    against it, so both sides are normalised the same way: a stamp written by
    an aware writer (``Z`` / ``+00:00``) must not compare as a different
    instant from one written by ``_utcnow()``, which is naive.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return _coerce_naive_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


async def _census_exact_target_reviews(
    session: AsyncSession,
    *,
    project_id,
    pr_number: int,
    chain_id: str,
    base_sha: str,
    head_sha: str,
    driver_reset_at: object = None,
) -> tuple[int, dict[str, object]]:
    """Count review ROWS this chain has already spent on one immutable target.

    Returns ``(consumed, census)``.  ``consumed`` is the number of review tasks
    for this project + PR that were spawned against exactly ``(base_sha,
    head_sha)``, belong to chain ``chain_id``, have already reported, and count
    under ``task_counts_toward_review_cap`` -- the single source of truth the
    per-PR iteration cap already uses.  ``census`` explains the number: what was
    counted, what is still in flight, and why every excluded row was excluded.

    Two defects are closed by deriving this from rows rather than from a
    counter on the driver task's context:

    * the count survives the ci-waiting driver row migrating (see the comment
      on ``_SPAWN_BUDGET_COUNT_KEY``); and
    * budget is spent by adjudication, not by creation.  A review that died
      infra-class (AGENT_TIMEOUT / PROVISION_FAILURE / RATE_LIMIT /
      UNPLACEABLE_EXECUTION_SITE / ...) or was discarded as a stale-target
      respawn never read the diff, and ``task_counts_toward_review_cap``
      excludes exactly that class.  Charging it at creation time made five
      infra deaths park the PR with a message asserting five adjudications that
      never happened.

    In-flight reviews are uncounted for free: they are not in
    ``_REVIEW_ADJUDICATION_CONSUMED_STATUSES``.

    An operator ``reset-iteration-cap`` closes the episode: rows created at or
    before its ``iteration_reset_at`` stamp are excluded, exactly as the per-PR
    cap query in ``review_pipeline`` excludes them.  That endpoint used to
    release this budget by popping the counter out of the parking task's
    context; with the count derived from rows there is no counter to pop, so the
    release has to be a window over rows or the operator's explicit disposition
    would be a no-op and the next CI tick would re-park the PR they just freed.
    The stamp is read both from the driving task and from the censused review
    rows themselves, because the parking row an operator resets is normally one
    of those rows and ``_spawn_pipeline_task`` does not propagate the stamp into
    the children it mints -- reading it off the rows keeps the window alive
    across the very driver migration this census exists to survive.
    """
    from app.review_pipeline import task_counts_toward_review_cap

    # Push project + PR + type into SQL.  ``ix_task_payloads_pr_number``
    # (alembic 0057) is an expression index on ``(payloads -> 0 ->> 'pr_number')``
    # and Postgres needs the byte-exact ``.as_string()`` form to use it; SQLite
    # has no such index and needs ``func.json_extract`` to read the value as a
    # primitive.  Same dialect split the per-PR cap query in ``review_pipeline``
    # uses, for the same reason.
    if resolve_dialect_name(session) == "sqlite":
        pr_number_match = (
            func.json_extract(Task.payloads, "$[0].pr_number") == pr_number
        )
        type_match = func.json_extract(Task.payloads, "$[0].type") == "review"
    else:
        pr_number_match = Task.payloads[0]["pr_number"].as_string() == str(pr_number)
        type_match = Task.payloads[0]["type"].as_string() == "review"

    rows = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.context,
                        Task.payloads,
                        Task.archived,
                        Task.project_id,
                        Task.created_at,
                        raiseload=True,
                    )
                )
                .where(
                    Task.project_id == project_id,
                    pr_number_match,
                    type_match,
                )
            )
        )
        .scalars()
        .all()
    )

    reset_candidates: list[datetime] = []
    parsed_driver_reset = _parse_reset_at(driver_reset_at)
    if parsed_driver_reset is not None:
        reset_candidates.append(parsed_driver_reset)
    for row in rows:
        row_loop = (row.context or {}).get("review_loop")
        if not isinstance(row_loop, dict):
            continue
        parsed_row_reset = _parse_reset_at(row_loop.get("iteration_reset_at"))
        if parsed_row_reset is not None:
            reset_candidates.append(parsed_row_reset)
    reset_at = max(reset_candidates) if reset_candidates else None

    counted_ids: list[str] = []
    in_flight_ids: list[str] = []
    excluded: dict[str, int] = {}

    def _exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for row in rows:
        row_loop = (row.context or {}).get("review_loop") or {}
        if not isinstance(row_loop, dict):
            _exclude("malformed_review_loop")
            continue
        row_target = row_loop.get("review_target")
        if not isinstance(row_target, dict):
            _exclude("no_review_target")
            continue
        if (
            str(row_target.get("base_sha") or "") != base_sha
            or str(row_target.get("head_sha") or "") != head_sha
        ):
            _exclude("different_target")
            continue
        row_chain = str(row_loop.get("original_task_id") or "") or str(
            (row.first_payload or {}).get("original_task_id") or ""
        )
        if row_chain != chain_id:
            _exclude("other_chain")
            continue
        if reset_at is not None and (
            row.created_at is None or _coerce_naive_utc(row.created_at) <= reset_at
        ):
            # Closed episode: an operator adjudicated the exhaustion itself.
            _exclude("before_operator_reset")
            continue
        if row.status not in _REVIEW_ADJUDICATION_CONSUMED_STATUSES:
            in_flight_ids.append(str(row.id))
            continue
        if not task_counts_toward_review_cap(row):
            # Infra death, stale-target discard, or an operator iteration-cap
            # reset (which archives the row).  None of them adjudicated the
            # diff, so none of them may spend a slot.
            _exclude("not_cap_counting")
            continue
        counted_ids.append(str(row.id))

    census: dict[str, object] = {
        "consumed": len(counted_ids),
        "consumed_task_ids": sorted(counted_ids)[:_CENSUS_TASK_ID_SAMPLE],
        "in_flight": len(in_flight_ids),
        "in_flight_task_ids": sorted(in_flight_ids)[:_CENSUS_TASK_ID_SAMPLE],
        "excluded": excluded,
        "review_rows_examined": len(rows),
        "chain_id": chain_id,
        "target": {"base_sha": base_sha, "head_sha": head_sha},
        "counted_at": _utcnow().isoformat(),
        **({"episode_reset_at": reset_at.isoformat()} if reset_at else {}),
    }
    return len(counted_ids), census


def review_target_matches_merge_identity(
    review_target: dict[str, str | int] | None,
    *,
    project_id: str,
    pr_number: int,
) -> bool:
    """Return whether a purported immutable target belongs to this merge."""
    if not isinstance(review_target, dict):
        return False
    try:
        target_pr_number = int(review_target.get("pr_number") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        project_id
        and str(review_target.get("project_id") or "") == project_id
        and target_pr_number == int(pr_number)
        and str(review_target.get("base_sha") or "")
        and str(review_target.get("head_sha") or "")
    )


async def _enforce_authoritative_review_merge_gate(
    task: Task,
    session: AsyncSession,
    *,
    ci_loop: dict,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_task_id: str,
    live_review_target: dict[str, str | int] | None,
) -> bool:
    """Require empty binding blockers and latest exact-target APPROVE."""
    raw_history = ci_loop.get("review_history") or []
    history = raw_history if isinstance(raw_history, list) else []
    persisted_ledger = ci_loop.get("issue_ledger")
    try:
        ledger = build_issue_ledger(
            history,
            **(
                {"seed_ledger": persisted_ledger}
                if persisted_ledger is not None
                else {}
            ),
            stamp_complete=True,
        )
    except ValueError:
        ledger = build_issue_ledger(history, stamp_complete=True)
    ledger_proof_complete = issue_ledger_complete_for_history(ledger, history)
    if not ledger_proof_complete and len(history) >= REVIEW_HISTORY_MAX_ENTRIES:
        ledger["open"].append(
            {
                "issue_id": LEDGER_PROOF_INCOMPLETE_ISSUE_ID,
                "severity": "major",
                "description": "durable blocker ledger missing after history compaction",
            }
        )
    open_blockers = list(ledger.get("open") or [])

    # Every dictionary here is review evidence.  A trailing COMMENT-only,
    # feedback-only, or malformed row invalidates an older APPROVE.
    latest_review = next(
        (entry for entry in reversed(history) if isinstance(entry, dict)),
        {},
    )
    latest_is_approval = (
        str(latest_review.get("verdict") or "").upper() == "APPROVE"
    )
    approved_head = str(
        latest_review.get("reviewed_head_sha")
        or latest_review.get("head_sha")
        or ""
    )
    approved_base = str(
        latest_review.get("reviewed_base_sha")
        or latest_review.get("base_sha")
        or ""
    )
    live_head = str((live_review_target or {}).get("head_sha") or "")
    live_base = str((live_review_target or {}).get("base_sha") or "")
    project_id = str(task.project_id) if task.project_id else ""
    target_identity_is_exact = review_target_matches_merge_identity(
        live_review_target,
        project_id=project_id,
        pr_number=pr_number,
    )
    approval_is_exact = bool(
        latest_is_approval
        and latest_review.get("target_verified_by_backend") is True
        and str(latest_review.get("review_task_id") or "").strip()
        and ledger_proof_complete
        and target_identity_is_exact
        and approved_head
        and approved_base
        and approved_head == live_head
        and approved_base == live_base
    )
    if not open_blockers and approval_is_exact:
        for transient_key in (
            "authoritative_review_required",
            "authoritative_review_target_unresolved_at",
            "authoritative_review_task_id",
            "review_not_required",
            "review_policy_reason",
        ):
            ci_loop.pop(transient_key, None)
        ci_loop["review_approved"] = True
        ci_loop["review_target"] = dict(live_review_target or {})
        ci_loop["head_sha"] = live_head
        ci_loop["issue_ledger"] = ledger
        task.context = {**(task.context or {}), "review_loop": ci_loop}
        flag_modified(task, "context")
        return True

    from app.review_pipeline import _spawn_pipeline_task

    # Spawn helpers may commit/rollback and expire ORM instances, so snapshot
    # everything needed to reconstruct the fail-closed gate first.
    source_task_id = task.id
    source_context = dict(task.context or {})
    source_prompt = task.first_payload.get("prompt", "") or (task.description or "")
    gated_loop = dict(ci_loop)
    for stale_key in (
        "review_not_required",
        "review_policy_reason",
        "verdict",
        "authoritative_review_task_id",
    ):
        gated_loop.pop(stale_key, None)
    gated_loop["review_approved"] = False
    gated_loop["authoritative_review_required"] = True
    gated_loop["ci_waiting"] = True
    gated_loop["issue_ledger"] = ledger
    if not gated_loop.get("review_wait_started"):
        gated_loop["review_wait_started"] = _utcnow().isoformat()
    if target_identity_is_exact:
        gated_loop["authoritative_review_target"] = dict(live_review_target or {})
        gated_loop.pop("authoritative_review_target_unresolved_at", None)
    else:
        gated_loop.pop("authoritative_review_target", None)
        gated_loop["authoritative_review_target_unresolved_at"] = (
            _utcnow().isoformat()
        )
    task.context = {**source_context, "review_loop": gated_loop}
    flag_modified(task, "context")

    # There used to be an "unsatisfiable ledger" escalation here.  It fired
    # when the durable ledger carried a lineage marker, was stamped
    # ``complete=False``, and the retained history sat at its cap -- a state
    # ``build_issue_ledger`` could never leave, so the synthetic
    # ``BP-ledger-proof-incomplete`` blocker was re-minted on every pass and the
    # PR was handed to an operator to adjudicate a ledger no commit could fix
    # and no review could refute.
    #
    # That state no longer exists: ``build_issue_ledger`` now RECOVERS a seed
    # whose marker has fallen out of the window by replaying the whole retained
    # window and re-anchoring the marker to the new tail, at any window size.
    # Keeping the detector would be worse than deleting it -- its rule
    # (marker + not complete + at cap) no longer implies permanence, so it would
    # escalate a state that now self-heals on the very next build.  The gate
    # below is what bounds a genuinely stalled loop, and it is keyed on forward
    # motion rather than on a buffer size.

    # Bound the exact-target respawn.  Keyed on the immutable target, so the
    # only thing this can ever stop is a loop in which nothing is moving:
    # any real forward motion mints a new key and restores the whole budget.
    #
    # The count is derived from review task ROWS, never from a counter carried
    # on this task's context.  ``task`` is whichever ci-waiting row the CI
    # monitor happened to drive this tick, and that row changes underneath the
    # loop -- see ``_SPAWN_BUDGET_COUNT_KEY``.  Rows do not.
    spawn_target_key = f"{live_base}:{live_head}"
    # Mirror ``_spawn_pipeline_task``'s ``resolved_original_id`` exactly, so the
    # chain this census counts is the chain the spawn will stamp on the row it
    # creates.
    chain_id = (
        str(original_task_id or "")
        or str(ci_loop.get("original_task_id") or "")
        or str(task.first_payload.get("original_task_id") or "")
        or str(source_task_id)
    )
    prior_spawns = 0
    spawn_census: dict[str, object] = {}
    if target_identity_is_exact:
        prior_spawns, spawn_census = await _census_exact_target_reviews(
            session,
            project_id=task.project_id,
            pr_number=pr_number,
            chain_id=chain_id,
            base_sha=live_base,
            head_sha=live_head,
            driver_reset_at=ci_loop.get("iteration_reset_at"),
        )
        gated_loop[_SPAWN_BUDGET_TARGET_KEY] = spawn_target_key
        gated_loop[_SPAWN_BUDGET_COUNT_KEY] = prior_spawns
        gated_loop[_SPAWN_BUDGET_CENSUS_KEY] = spawn_census
        task.context = {**source_context, "review_loop": gated_loop}
        flag_modified(task, "context")
    if (
        target_identity_is_exact
        and prior_spawns >= _EXACT_TARGET_REVIEW_SPAWN_BUDGET
    ):
        task.context = {
            **source_context,
            "review_loop": gated_loop,
            REVIEW_LOOP_ABANDON_CANDIDATE_KEY: {
                "status": "abandon_candidate",
                "reason": "exact_target_review_budget_exhausted",
                "scope": "exact-target-review",
                "pr_number": pr_number,
                "detail": (
                    f"{prior_spawns} exact-target review(s) have already "
                    "adjudicated the immutable target "
                    f"{live_base[:8]}..{live_head[:8]} on this chain and the "
                    "binding blocker ledger is still not clear. The head has "
                    "not moved, so every further review reads the identical "
                    "diff and can only re-derive the same verdict. An operator "
                    "must adjudicate the open blockers, or a fix must land a "
                    "commit -- which mints a new target and restores the "
                    "budget on its own. Reviews that never read the diff "
                    "(infra deaths, stale-target discards, in-flight rows) are "
                    "excluded from the count; see "
                    f"``{_SPAWN_BUDGET_CENSUS_KEY}`` for the per-row reasons."
                ),
                "open_issue_ids": [
                    str(blocker.get("issue_id") or "")
                    for blocker in open_blockers
                    if isinstance(blocker, dict)
                ],
                "review_history_entries": len(history),
                "spawned_reviews": prior_spawns,
                "adjudicated_reviews": prior_spawns,
                "budget": _EXACT_TARGET_REVIEW_SPAWN_BUDGET,
                "spawn_census": spawn_census,
                "review_target": {
                    "base_sha": live_base,
                    "head_sha": live_head,
                },
                "recorded_at": _utcnow().isoformat(),
            },
        }
        flag_modified(task, "context")
        _excluded_total = sum(
            int(count)
            for count in (spawn_census.get("excluded") or {}).values()  # type: ignore[union-attr]
        )
        task.latest_status_message = (
            f"Merge held for PR #{pr_number}: {prior_spawns} exact-target "
            "reviews of an unchanged head have not cleared "
            f"{len(open_blockers)} binding blocker(s); no further review can "
            "read a different diff. "
            f"({spawn_census.get('in_flight', 0)} in flight, {_excluded_total} "
            "row(s) excluded as never having adjudicated this diff.) "
            "Operator adjudication required."
        )
        session.add(task)
        logger.error(
            "[CI] Merge held for PR #%s: exact-target review budget exhausted "
            "(%d/%d adjudicated reviews at %s..%s on chain %s, %d open "
            "blocker(s); %d in flight, %s excluded); suppressed the respawn "
            "and escalated to operator adjudication",
            pr_number,
            prior_spawns,
            _EXACT_TARGET_REVIEW_SPAWN_BUDGET,
            live_base[:8],
            live_head[:8],
            chain_id,
            len(open_blockers),
            spawn_census.get("in_flight", 0),
            spawn_census.get("excluded") or {},
        )
        return False

    review_task_id = ""
    if target_identity_is_exact:
        review_task, _spawn_created = await _spawn_pipeline_task(
            task,
            "review",
            session,
            pr_number,
            branch,
            base_branch,
            original_prompt=source_prompt,
            original_task_id=original_task_id,
            review_target=live_review_target,
        )
        if review_task is not None:
            review_task_id = str(review_task.id)
        # No budget is charged here, deliberately.  Creation is not
        # adjudication: the row this spawn just minted may die in provisioning,
        # time out, be superseded, or be discarded as a stale-target respawn --
        # none of which ever read the diff.  ``_census_exact_target_reviews``
        # re-derives the count from rows on the next pass and
        # ``task_counts_toward_review_cap`` drops exactly that class, so a slot
        # is spent only once a review has actually reported.  Charging at
        # creation made five infra deaths park the PR with a status message
        # asserting five adjudications that never happened.
        if _spawn_created:
            logger.info(
                "[CI] PR #%s: spawned exact-target review %s at %s..%s "
                "(%d/%d adjudicated on chain %s so far); the slot is charged "
                "when it reports, not now",
                pr_number,
                review_task_id or "<none>",
                live_base[:8],
                live_head[:8],
                prior_spawns,
                _EXACT_TARGET_REVIEW_SPAWN_BUDGET,
                chain_id,
            )

    post_spawn_context = task.__dict__.get("context")
    refreshed_task = (
        task
        if isinstance(post_spawn_context, dict)
        else await session.get(Task, source_task_id, populate_existing=True)
    )
    if refreshed_task is None:
        logger.error(
            "[CI] Merge held for PR #%s but source task %s disappeared after "
            "the authoritative review spawn attempt",
            pr_number,
            source_task_id,
        )
        return False

    refreshed_context = dict(refreshed_task.context or {})
    refreshed_loop = dict(refreshed_context.get("review_loop") or {})
    refreshed_loop.update(gated_loop)
    for stale_key in ("verdict", "review_not_required", "review_policy_reason"):
        refreshed_loop.pop(stale_key, None)
    if review_task_id:
        refreshed_loop["authoritative_review_task_id"] = review_task_id
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: authoritative blockers/proof "
            f"require exact-target review {review_task_id}"
        )
    elif not target_identity_is_exact:
        refreshed_loop.pop("authoritative_review_task_id", None)
        refreshed_loop.pop("authoritative_review_target", None)
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: could not resolve the live "
            "review target; retrying before merge"
        )
    else:
        refreshed_loop.pop("authoritative_review_task_id", None)
        refreshed_task.latest_status_message = (
            f"Merge held for PR #{pr_number}: authoritative blockers/proof "
            "require an exact-target review"
        )
    refreshed_task.context = {
        **refreshed_context,
        "review_loop": refreshed_loop,
    }
    flag_modified(refreshed_task, "context")
    session.add(refreshed_task)
    logger.warning(
        "[CI] Merge held for PR #%s: %d authoritative blocker(s), "
        "exact approval=%s; exact-target review required",
        pr_number,
        len(open_blockers),
        approval_is_exact,
    )
    return False
