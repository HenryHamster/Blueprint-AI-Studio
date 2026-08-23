"""Bounded operator-disposition window for a parked review-fix chain.

THE DEFECT. Two different code paths park a review-fix chain "for operator
disposition" and then never decide:

* ``review_pipeline._halt_for_cross_episode_abandon_candidate`` writes
  ``context["review_loop_abandon_candidate"]`` plus an independent
  ``cross_episode_abandon_candidate`` approval hold; and
* the stuck-cycle / standoff detector writes an unresolved
  ``context["review_loop_terminal_outcome"]`` of kind ``stuck``/``standoff``.

Either one makes :func:`app.review_loop_convergence.review_loop_parked_for_operator`
return a park reason FOREVER. The only documented releases are a human calling
``POST /api/admin/tasks/{id}/reset-iteration-cap``, a later APPROVE/merge (the
loop is stopped, so one can never arrive), or the stale-head release (dead once
nothing is pushing to the branch). So a non-converging loop parks permanently,
its rollup parent sits ``EVALUATING`` forever, and the whole research direction
stalls.

Live instances on 2026-08-23: three stranded rows at 7h / 15h / 10h, including
task ``a72ca0d6`` / rollup PR #17690, parked ``standoff`` at head
``627e5748`` — a head identical to the live branch tip, so the stale-head
escape hatch provably could never fire.

This is the "no gate that revokes authority and escalates to a human because a
bounded budget overflowed" rule. The park is not wrong; the park being
UNBOUNDED is.

THE FIX. The park now has a bounded operator-disposition WINDOW. Inside it the
operator owns the row exactly as before. When it expires with no disposition,
the engine DECIDES — abandon — and the chain reaches a terminal instead of
waiting for a person who is not coming.

WHAT DISPOSITION MEANS HERE, precisely:

* the task terminalizes ``FAILED`` with
  ``context["failure_detail"] = review_park_abandoned``;
* a ``disposition`` dict is nested INSIDE the park marker it decided (the rest
  of the marker is preserved — it is the audit trail);
* the approval hold is released ONLY when this sweep provably owns it; and
* **the PR is never closed, merged, or otherwise touched.** See
  :func:`_never_touch_the_pr` for the incident that rule exists for.

FAIL CLOSED ON MISSING PROVENANCE. A park whose ``recorded_at`` is missing or
unparseable cannot be proven expired, so it is SKIPPED and stays parked. This
mirrors the identical reasoning already documented in
:func:`app.review_loop_convergence.review_loop_park_stale_head` for a park with
no recorded ``head_sha``.

STALE-HEAD PRECEDENCE. A stuck/standoff park whose recorded ``head_sha``
differs from the chain's last-observed live head is PROVABLY STALE — a fix
landed that the park never saw. That row belongs to the existing stale-head
release path, not to this sweep, and disposing it would abandon work that
already landed. It is skipped even when expired.

NEVER MASQUERADE AS CONVERGENCE. This sweep deliberately does NOT call
``clear_review_escalation_approval_gate``. That helper stamps
``resolved: True`` / ``resolved_by`` on the terminal outcome, whose established
meaning across every consumer is "the loop demonstrably CONVERGED" (a later
APPROVE, or a merge). Abandoning is the opposite conclusion. A false
``resolved: True`` would make an abandoned row look healthy to every reader of
that breadcrumb, so the disposition is stamped under its own key and
``resolved`` is left untouched.

NO ENABLE FLAG. The window is defaulted IN CODE and ships ON. The runtime-config
key ``review_park_disposition_window_hours`` only RETUNES the live value inside
a sane band; no host env var, absent key, or corrupt config can disable the
sweep or make the window effectively infinite.
"""

import logging
from datetime import datetime, timezone

# NOTE: every ``app.*`` import in this module is deliberately LAZY (inside the
# function that needs it), matching the convention the sibling bounded-window
# module ``recovery_evaluating_handmerge_latch`` uses. ``app.reaper`` imports the
# sweeps, and the park constants live in ``review_loop_convergence`` which sits
# deep in the review-pipeline import graph, so keeping the module-level imports
# stdlib-only makes this module safe to import from anywhere.

logger = logging.getLogger(__name__)

# ── The window: ON in code, retunable, never gate-able off ───────────────────

REVIEW_PARK_DISPOSITION_WINDOW_HOURS = 12
REVIEW_PARK_DISPOSITION_WINDOW_CONFIG_KEY = "review_park_disposition_window_hours"

# Nested INSIDE the park marker (the abandon-candidate dict, or the terminal
# outcome dict) so the decision travels with the evidence it was made from.
REVIEW_PARK_DISPOSED_KEY = "disposition"

# The third non-convergence terminal marker, alongside ``review_cap_exhausted``
# and ``fix_loop_diverged``. Unlike ``review_cap_exhausted``, THE PR IS STILL
# OPEN when this one is set.
REVIEW_PARK_ABANDONED_FAILURE_DETAIL = "review_park_abandoned"

# Batch bound, matching the sibling reaper sweeps: a wedge must not turn one
# cycle into an unbounded scan, and a poison row must not lose the sweep.
REVIEW_PARK_DISPOSITION_BATCH_LIMIT = 50

_DISPOSED_BY = "autonomous_disposition"
_DISPOSED_DECISION = "abandon"

# Park shapes this sweep decides.
_SHAPE_ABANDON_CANDIDATE = "abandon_candidate"
_SHAPE_TERMINAL_OUTCOME = "terminal_outcome"

# A park only holds up work while the row is still non-terminal. An already
# FAILED / COMPLETED / CANCELLED row has nothing left to decide.
_DISPOSABLE_STATUS_NAMES = ("QUEUED", "IN_PROGRESS", "EVALUATING", "BLOCKED")


def resolve_review_park_disposition_seconds(runtime_config) -> int:
    """Disposition window in seconds, defaulting IN CODE to the active value.

    Mirrors the defensive read of the sibling bounded window
    (``resolve_handmerge_redecide_seconds``): a corrupted
    ``runtime_config_json`` must retune nothing and must never abort the sweep.
    The 1..168h clamp is what stops a nonsense value from silently restoring the
    unbounded park by making the window astronomically large.
    """
    try:
        raw = (runtime_config or {}).get(
            REVIEW_PARK_DISPOSITION_WINDOW_CONFIG_KEY,
            REVIEW_PARK_DISPOSITION_WINDOW_HOURS,
        )
        hours = int(raw)
        if not (1 <= hours <= 168):
            hours = REVIEW_PARK_DISPOSITION_WINDOW_HOURS
    except (ValueError, TypeError, AttributeError):
        hours = REVIEW_PARK_DISPOSITION_WINDOW_HOURS
    return hours * 3600


def _never_touch_the_pr() -> None:
    """Documentation anchor: this sweep NEVER closes or merges the PR.

    Incident (lineage ``d3c12745`` / PR #16759, 2026-08-06): after a stuck
    escalation parked a chain, every rollup sweep kept respawning a review, the
    recount ratcheted ``pr_iterations`` to the global cap, and the cap handler
    AUTO-CLOSED the PR the park had deliberately preserved — destroying a real
    landed fix across 19 burned review tasks. That is exactly the authority a
    disposition sweep must not take. Abandoning the TASK is a statement about
    the autonomous loop's budget; it is not a judgement that the diff is
    worthless. The PR is left OPEN for a human or a later re-plan, and the
    status message says so.
    """


def _parse_recorded_at(raw) -> datetime | None:
    """Parse a stored ISO timestamp to naive UTC, or ``None`` if unusable.

    ``None`` is the fail-closed answer: the caller must SKIP the row, never
    treat an unreadable stamp as "expired long ago".
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _open_finding_count(ctx: dict) -> int | None:
    """Open issue-ledger findings on the chain, or ``None`` when unrecorded."""
    review_loop = ctx.get("review_loop")
    if not isinstance(review_loop, dict):
        return None
    ledger = review_loop.get("issue_ledger")
    if not isinstance(ledger, dict):
        return None
    open_findings = ledger.get("open")
    if not isinstance(open_findings, list):
        return None
    return len(open_findings)


def _coerce_pr_number(candidate) -> int | None:
    if candidate is None:
        return None
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def _park_pr_number(ctx: dict, marker: dict) -> int | None:
    review_loop = ctx.get("review_loop")
    review_loop = review_loop if isinstance(review_loop, dict) else {}
    for candidate in (
        marker.get("pr_number"),
        review_loop.get("pr_number"),
        ctx.get("rollup_pr_number"),
    ):
        resolved = _coerce_pr_number(candidate)
        if resolved is not None:
            return resolved
    return None


def _skip(reason: str, **extra) -> dict:
    return {"action": "skip", "reason": reason, **extra}


def _abandon_candidate_why(ctx: dict, marker: dict) -> str:
    reason = str(marker.get("reason") or marker.get("status") or "abandon_candidate")
    parts = [
        f"{marker.get('attempts', '?')} attempt(s)",
        f"{marker.get('cycles', '?')} cycle(s)",
    ]
    open_count = _open_finding_count(ctx)
    if open_count is not None:
        parts.append(f"{open_count} finding(s) still open")
    detail = str(marker.get("detail") or "").strip()
    why = f"review-fix loop did not converge ({reason}, {', '.join(parts)})"
    return f"{why}: {detail}" if detail else why


def _terminal_outcome_why(ctx: dict, outcome: dict) -> str:
    kind = str(outcome.get("kind") or "park")
    reason = str(outcome.get("reason") or kind)
    parts = [f"{outcome.get('iterations', '?')} iteration(s)"]
    open_count = _open_finding_count(ctx)
    if open_count is not None:
        parts.append(f"{open_count} finding(s) still open")
    detail = str(outcome.get("detail") or "").strip()
    why = (
        f"review-fix loop parked at {kind} and did not converge "
        f"({reason}, {', '.join(parts)})"
    )
    return f"{why}: {detail}" if detail else why


def classify_review_park(task, *, now: datetime, window_seconds: int) -> dict:
    """Decide whether ``task``'s park has expired and may be disposed.

    Pure and read-only, so the sweep can run it once cheaply on an unlocked row
    and again under the row lock before mutating anything. Returns either
    ``{"action": "skip", "reason": ...}`` or ``{"action": "dispose", ...}`` with
    everything the mutation needs.
    """
    from app.review_loop_convergence import (
        REVIEW_LOOP_ABANDON_CANDIDATE_KEY,
        REVIEW_LOOP_PARKING_OUTCOME_KINDS,
        REVIEW_LOOP_TERMINAL_OUTCOME_KEY,
    )

    ctx = task.context or {}
    status_name = getattr(task.status, "value", task.status)
    if str(status_name) not in _DISPOSABLE_STATUS_NAMES:
        return _skip("already_terminal")
    if task.archived:
        return _skip("archived")

    # Shape precedence mirrors ``review_loop_parked_for_operator``: the
    # abandon-candidate marker is checked first there, so it is the park that is
    # actually suppressing respawn when both are present.
    marker = ctx.get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY)
    if isinstance(marker, dict):
        shape = _SHAPE_ABANDON_CANDIDATE
        container_key = REVIEW_LOOP_ABANDON_CANDIDATE_KEY
        park_reason = str(marker.get("reason") or marker.get("status") or shape)
        why = _abandon_candidate_why(ctx, marker)
    else:
        outcome = ctx.get(REVIEW_LOOP_TERMINAL_OUTCOME_KEY)
        if not isinstance(outcome, dict):
            return _skip("not_parked")
        if outcome.get("resolved"):
            # A later APPROVE or merge already proved this loop converged and
            # released the park. There is nothing left to decide, and deciding
            # anyway would abandon a healthy chain.
            return _skip("already_resolved")
        if outcome.get("kind") not in REVIEW_LOOP_PARKING_OUTCOME_KINDS:
            # repeat_fingerprint_halt / supersession_churn_halt raise a hold but
            # do NOT stop the loop, so they are not parks.
            return _skip("kind_not_parking")
        marker = outcome
        shape = _SHAPE_TERMINAL_OUTCOME
        container_key = REVIEW_LOOP_TERMINAL_OUTCOME_KEY
        park_reason = str(outcome.get("kind"))
        why = _terminal_outcome_why(ctx, outcome)

    if isinstance(marker.get(REVIEW_PARK_DISPOSED_KEY), dict):
        # Already decided. Never re-decide: a second pass would move the
        # recorded decision time and re-emit the operator NOTE.
        return _skip("already_disposed", shape=shape)

    recorded_at = _parse_recorded_at(marker.get("recorded_at"))
    if recorded_at is None:
        # FAIL CLOSED. A park without provenance cannot be proven expired, so it
        # stays parked — the same reasoning ``review_loop_park_stale_head``
        # applies to a park with no recorded ``head_sha``.
        return _skip("no_provenance", shape=shape)

    age_seconds = (now - recorded_at).total_seconds()
    if age_seconds < window_seconds:
        # The operator still owns it.
        return _skip("within_window", shape=shape, age_seconds=age_seconds)

    if shape == _SHAPE_TERMINAL_OUTCOME:
        # STALE-HEAD PRECEDENCE. Compared without a network fetch: the chain's
        # own last-observed live head vs the head the park was recorded against.
        # A difference proves a fix landed that this park never saw, which is
        # the existing stale-head release path's row, not ours — disposing it
        # would abandon work that already landed (PR #16759's lesson). Missing
        # either sha means we cannot PROVE staleness, and the window already
        # gave the operator their chance, so we dispose normally.
        parked_head = str(marker.get("head_sha") or "")
        review_loop = ctx.get("review_loop")
        live_head = str(
            (review_loop or {}).get("head_sha") or ""
            if isinstance(review_loop, dict)
            else ""
        )
        if parked_head and live_head and parked_head != live_head:
            return _skip(
                "stale_head_not_expired",
                shape=shape,
                parked_head=parked_head,
                live_head=live_head,
            )

    return {
        "action": "dispose",
        "shape": shape,
        "container_key": container_key,
        "park_reason": park_reason,
        "why": why,
        "pr_number": _park_pr_number(ctx, marker),
        "age_seconds": age_seconds,
    }


def _abandon_hold_is_owned(task, ctx: dict) -> bool:
    """Whether the ``cross_episode_abandon_candidate`` hold is exactly ours.

    Byte-for-byte the ownership predicate ``admin.reset_iteration_cap`` uses for
    the same hold (see its ``cap_hold_owned``), minus the cap-handler owners it
    also accepts. A newer independent hold may share the boolean latch but is
    NOT this sweep's authority to consume.
    """
    from app.admin_approval_authority import (
        APPROVAL_HOLD_EPISODE_KEY,
        APPROVAL_HOLD_PROVENANCE_KEY,
    )
    from app.review_loop_convergence import REVIEW_LOOP_ABANDON_CANDIDATE_KEY

    hold_provenance = ctx.get(APPROVAL_HOLD_PROVENANCE_KEY)
    hold_reason = ctx.get("approval_hold_reason")
    return bool(
        task.requires_approval is True
        and isinstance(hold_provenance, dict)
        and hold_provenance.get("episode") == ctx.get(APPROVAL_HOLD_EPISODE_KEY)
        and hold_provenance.get("set_at") == ctx.get("_approval_set_at")
        and hold_provenance.get("reason") == hold_reason
        and hold_provenance.get("owner") == "review_pipeline"
        and hold_reason == "cross_episode_abandon_candidate"
        and isinstance(ctx.get(REVIEW_LOOP_ABANDON_CANDIDATE_KEY), dict)
    )


def _terminal_outcome_hold_is_owned(task, ctx: dict, outcome: dict) -> bool:
    """Whether the stuck/standoff escalation hold is exactly ours.

    Reuses ``clear_review_escalation_approval_gate``'s comparison rather than
    inventing one: the breadcrumb's bound ``approval_hold_episode`` must equal
    the row's CURRENT episode identity, with the same rolling-compatibility
    branch for pre-provenance rows (clearable only while no newer-code episode
    and no independent reason displaced the old breadcrumb).

    When ``requires_approval`` is False there is simply no hold to consume —
    that is the NORMAL path for this shape (the live 2026-08-23 standoff row had
    gates off), not an error.
    """
    if not task.requires_approval:
        return False
    from app.admin_approval_authority import APPROVAL_HOLD_PROVENANCE_KEY
    from app.review_loop_convergence import _approval_hold_episode_identity
    from app.spawn_calibration import SPEND_BUDGET_APPROVAL_HOLD_REASON

    if ctx.get("approval_hold_reason") == SPEND_BUDGET_APPROVAL_HOLD_REASON:
        return False
    expected_hold = outcome.get("approval_hold_episode")
    current_hold = _approval_hold_episode_identity(task)
    if isinstance(expected_hold, dict):
        return current_hold == expected_hold
    if current_hold is not None or ctx.get(APPROVAL_HOLD_PROVENANCE_KEY):
        return False
    return ctx.get("approval_hold_reason") in (
        None,
        outcome.get("kind"),
        outcome.get("reason"),
    )


def _release_owned_hold(task, *, shape: str, container_key: str) -> bool:
    """Release the approval hold iff this sweep provably owns it.

    Returns whether it was released. When it is NOT owned the hold is left
    completely alone (a newer independent hold is not ours to consume) and the
    caller still terminalizes — the log line says which happened.
    """
    from app.admin_approval_authority import (
        APPROVAL_HOLD_EPISODE_KEY,
        APPROVAL_HOLD_PROVENANCE_KEY,
        approval_hold_provenance,
        consume_approval_hold_metadata,
        revoke_operator_approval_followup,
    )
    from app.closed_pr_reconciliation import revoke_review_wait_approval_hold

    ctx = dict(task.context or {})
    if shape == _SHAPE_ABANDON_CANDIDATE:
        if not _abandon_hold_is_owned(task, ctx):
            return False
        expected_episode = approval_hold_provenance(task)
        task.requires_approval = False
        revoke_review_wait_approval_hold(task)
        revoke_operator_approval_followup(task)
        consume_approval_hold_metadata(task, expected_episode=expected_episode)
        return True

    outcome = ctx.get(container_key)
    outcome = outcome if isinstance(outcome, dict) else {}
    if not _terminal_outcome_hold_is_owned(task, ctx, outcome):
        return False
    task.requires_approval = False
    revoke_review_wait_approval_hold(task)
    revoke_operator_approval_followup(task)
    # Pop the exact episode metadata by hand, exactly as
    # ``clear_review_escalation_approval_gate`` does for this shape.
    # ``consume_approval_hold_metadata`` is deliberately NOT used here: it also
    # pops ``review_loop_terminal_outcome``, which is the very breadcrumb the
    # disposition is about to be stamped into.
    ctx = dict(task.context or {})
    ctx.pop("_approval_set_at", None)
    ctx.pop("approval_hold_reason", None)
    ctx.pop(APPROVAL_HOLD_EPISODE_KEY, None)
    ctx.pop(APPROVAL_HOLD_PROVENANCE_KEY, None)
    task.context = ctx
    return True


def _park_marker_present_filter(session):
    """Dialect-conditional "row carries a park marker" WHERE clause.

    ``JSONB.has_key`` compiles to the Postgres ``?`` operator, which is a SQLite
    syntax error under the qmark paramstyle (proven, not assumed), so the SQLite
    branch uses ``json_extract`` — the same split ``landed_signal`` and
    ``_pr_number_match`` already make.
    """
    from sqlalchemy import func, or_

    from app.database import resolve_dialect_name
    from app.models import Task
    from app.review_loop_convergence import (
        REVIEW_LOOP_ABANDON_CANDIDATE_KEY,
        REVIEW_LOOP_TERMINAL_OUTCOME_KEY,
    )

    if resolve_dialect_name(session) == "sqlite":
        return or_(
            func.json_extract(
                Task.context, f"$.{REVIEW_LOOP_ABANDON_CANDIDATE_KEY}"
            ).is_not(None),
            func.json_extract(
                Task.context, f"$.{REVIEW_LOOP_TERMINAL_OUTCOME_KEY}"
            ).is_not(None),
        )
    return or_(
        Task.context.has_key(REVIEW_LOOP_ABANDON_CANDIDATE_KEY),
        Task.context.has_key(REVIEW_LOOP_TERMINAL_OUTCOME_KEY),
    )


async def _emit_disposition_note(session, task, *, pr_number, verdict: dict) -> None:
    """One deduped operator NOTE per disposed task. Never blocks the terminal.

    Mirrors ``review_loop_convergence._create_handmerge_attention_message``:
    idempotency-keyed, existence-checked, committed on its own so a message
    failure cannot roll back the disposition the caller already committed.
    """
    from sqlmodel import select as _sqlm_select

    from app.message_audience import stamp_message_audience
    from app.models import MessageType, ProjectMessage
    from app.project_message_creation import build_project_message

    ikey = f"review-park-disposed:{task.id}"
    existing = (
        await session.execute(
            _sqlm_select(ProjectMessage).where(
                ProjectMessage.project_id == task.project_id,
                ProjectMessage.idempotency_key == ikey,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    pr_label = f"PR #{pr_number}" if pr_number else "PR"
    session.add(
        await build_project_message(
            session,
            project_id=task.project_id,
            message_type=MessageType.NOTE,
            from_type="system",
            from_task_id=task.id,
            content=(
                f"{pr_label} abandoned by autonomous disposition — the "
                f"operator-disposition window expired with no decision: "
                f"{verdict['why']}. The PR was left OPEN; nothing closed or "
                f"merged it."
            ),
            context=stamp_message_audience(
                {
                    "pr_number": pr_number,
                    "task_id": str(task.id),
                    "park_reason": verdict["park_reason"],
                    "park_shape": verdict["shape"],
                },
                "review_park_disposed",
            ),
            idempotency_key=ikey,
        )
    )
    await session.commit()


async def _dispose_one(session, task_id, project_id, *, now, window_seconds) -> bool:
    """Lock, re-verify every precondition, and dispose exactly one park."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import Task, TaskStatus

    # Re-read and LOCK the exact row, then re-check EVERYTHING under the lock:
    # the unlocked classification above is only a candidate filter, and a
    # concurrent reset-iteration-cap / APPROVE / merge may have released the
    # park between the two reads. Same pattern as ``admin.reset_iteration_cap``.
    task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id, Task.project_id == project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if task is None:
        return False

    verdict = classify_review_park(task, now=now, window_seconds=window_seconds)
    if verdict["action"] != "dispose":
        logger.info(
            "[REAPER] Review-park disposition: skipped %s under lock (%s)",
            str(task_id)[:8],
            verdict["reason"],
        )
        return False

    shape = verdict["shape"]
    container_key = verdict["container_key"]
    window_hours = round(window_seconds / 3600, 2)

    released = _release_owned_hold(task, shape=shape, container_key=container_key)

    ctx = dict(task.context or {})
    container = dict(ctx.get(container_key) or {})
    # KEEP the rest of the marker — it is the audit trail for the decision.
    container[REVIEW_PARK_DISPOSED_KEY] = {
        "decided": _DISPOSED_DECISION,
        "at": now.isoformat(),
        "by": _DISPOSED_BY,
        "window_hours": window_hours,
        "why": verdict["why"],
    }
    # ``resolved``/``resolved_by`` are deliberately NOT set on a terminal
    # outcome: their established meaning is "the loop CONVERGED", and an
    # abandonment must never masquerade as convergence.
    ctx[container_key] = container
    ctx["failure_detail"] = REVIEW_PARK_ABANDONED_FAILURE_DETAIL
    task.context = ctx
    flag_modified(task, "context")

    pr_number = verdict["pr_number"]
    pr_label = f"PR #{pr_number}" if pr_number else "PR"
    task.status = TaskStatus.FAILED
    task.latest_status_message = (
        f"{pr_label} abandoned by autonomous disposition after {window_hours}h "
        f"with no operator disposition: {verdict['why']}. "
        f"PR left OPEN for a human or a later re-plan."
    )
    session.add(task)
    await session.commit()

    logger.info(
        "[REAPER] Review-park disposition: ABANDONED %s (%s, shape=%s, "
        "park_reason=%s, %s) — %s; PR left open",
        str(task_id)[:8],
        pr_label,
        shape,
        verdict["park_reason"],
        f"parked {round(verdict['age_seconds'] / 3600, 2)}h",
        (
            "released the approval hold this sweep owns"
            if released
            else "left the approval hold alone (not owned by this sweep)"
        ),
    )

    # The terminalization is already committed. A message failure must not undo
    # it, so this runs in its own guarded step.
    try:
        await _emit_disposition_note(
            session, task, pr_number=pr_number, verdict=verdict
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REAPER] Review-park disposition: NOTE write failed for %s: %s",
            str(task_id)[:8],
            exc,
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("[REAPER] Review-park disposition: rollback failed", exc_info=True)
    return True


async def sweep_dispose_expired_review_parks(session, now: datetime) -> int:
    """Decide every review park whose operator-disposition window has expired.

    Returns the number of rows disposed. Idempotent: a disposed row is FAILED
    (so it leaves the candidate set) AND carries a nested ``disposition``
    (so it is skipped even if something later re-opens it).
    """
    from sqlalchemy import select

    from app.models import Task, TaskStatus
    from app.shared_state import runtime_config

    window_seconds = resolve_review_park_disposition_seconds(runtime_config)

    disposable_statuses = [
        getattr(TaskStatus, name) for name in _DISPOSABLE_STATUS_NAMES
    ]
    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.archived.is_(False),
                    Task.status.in_(disposable_statuses),
                    _park_marker_present_filter(session),
                )
                .limit(REVIEW_PARK_DISPOSITION_BATCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    candidates = [
        (row.id, row.project_id)
        for row in rows
        if classify_review_park(row, now=now, window_seconds=window_seconds)["action"]
        == "dispose"
    ]
    if not candidates:
        return 0

    disposed = 0
    for task_id, project_id in candidates:
        # One row per transaction: a poison row must not lose the whole sweep.
        try:
            if await _dispose_one(
                session,
                task_id,
                project_id,
                now=now,
                window_seconds=window_seconds,
            ):
                disposed += 1
        except Exception as exc:  # noqa: PERF203 - poison-row isolation is the point
            logger.error(
                "[REAPER] Review-park disposition failed for %s: %s",
                str(task_id)[:8],
                exc,
                exc_info=True,
            )
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[REAPER] Review-park disposition: rollback failed", exc_info=True
                )

    if disposed:
        logger.info(
            "[REAPER] Review-park disposition: abandoned %d expired park(s) "
            "(window=%dh); every PR left OPEN",
            disposed,
            window_seconds // 3600,
        )
    return disposed

