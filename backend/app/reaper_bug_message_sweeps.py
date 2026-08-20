"""Bug-message convergence, resolution, and wake-delivery sweep helpers.

Extracted from reaper_sweeps.py (Steps 15, 17, 18, and the task-wake cluster).
All sweep entry points are re-exported from app.reaper_sweeps so existing
import sites require no changes.
"""

import contextlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, or_
from sqlmodel import select

from app.models import Task, TaskStatus, TaskWakeRegistration, WakeDeliveryState

logger = logging.getLogger(__name__)

# ── Step 15: linked bug-class message convergence / resurfacing sweep ────────
#
# A BUG_REPORT or BLOCKER message placed in 'linked' state has a durable forward
# link recorded in its JSONB context (see message_convergence.py). This sweep
# closes the loop: it reads the owning outcome's current lifecycle state and
# either terminally resolves the message (CONVERGENCE) or reopens it (RESURFACING).
#
# CONVERGENCE fires when the owning outcome actually *lands*:
#   - Direction: status == 'completed'
#   - Task: is_merged_completion(task) — COMPLETED AND carries the landed
#     signal (app.landed_signal). COMPLETED-but-not-landed is treated as
#     still in-flight, never converged.
#
# RESURFACING fires when the owning outcome ended without landing (failed,
# cancelled, retired, rejected, expired, superseded), went missing, or has been
# in-flight longer than _CONVERGENCE_RESURFACE_AFTER without delivering.
#
# Operator-resolved messages (status == 'resolved', not 'linked') are never
# touched — only 'linked' messages participate.  The sweep is idempotent: both
# the convergence and resurfacing transitions change the message status away from
# 'linked', so re-running on the same message is a no-op.

# Default time bound: a linked message whose owning outcome has not yet landed
# after this many days is resurfaced so the strategist can reassign the fix.
_CONVERGENCE_RESURFACE_AFTER = timedelta(days=7)

# Direction statuses that indicate the direction ended without landing the fix.
_DIRECTION_FAILED_STATUSES: frozenset[str] = frozenset(
    {"retired", "rejected", "expired", "superseded"}
)


def _converge_message(msg, now: datetime, rationale: str) -> None:
    """Stamp msg as resolved — the owning outcome landed.

    Mutates msg.status, msg.resolution, msg.resolved_at, and msg.context in
    place.  Does NOT call session.add() or commit.
    """
    msg.status = "resolved"
    msg.resolution = f"Owned outcome landed: {rationale}"
    msg.resolved_at = now
    ctx = dict(msg.context or {})
    ctx["_convergence_converged_at"] = now.isoformat()
    msg.context = ctx


def _resurface_message(msg, now: datetime, reason: str) -> None:
    """Reopen a linked bug-class message — owning outcome failed/missing/stale.

    Moves the stale forward link to ``_convergence_link_stale`` (audit trail),
    records why it came back, and sets status back to 'open'.
    Does NOT call session.add() or commit.
    """
    msg.status = "open"
    ctx = dict(msg.context or {})
    old_link = ctx.pop("_convergence_link", None)
    if old_link:
        ctx["_convergence_link_stale"] = old_link
    ctx["_convergence_resurfaced_at"] = now.isoformat()
    ctx["_convergence_resurfaced_reason"] = reason
    msg.context = ctx


async def sweep_converge_linked_bug_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
) -> int:
    """Step 15: reconcile linked BUG_REPORT/BLOCKER messages with owned outcomes.

    For each 'linked' bug-class message:
    - CONVERGE to 'resolved' when the owning outcome lands.
    - RESURFACE to 'open' when the owning outcome fails, is cancelled/retired/
      rejected/expired/superseded, goes missing, or has been in-flight for longer
      than _CONVERGENCE_RESURFACE_AFTER without delivering.
    - Leave untouched when the owning outcome is still legitimately in-flight
      within the time bound.

    Returns the number of messages acted on (converged + resurfaced).
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import Direction, ProjectMessage
    from app.message_convergence import BUG_CLASS_TYPES, LINKED_STATUS, get_message_link
    from app.landed_signal import is_merged_completion
    from app.project_message_projection import message_updated_event_payload
    from app.time_utils import _coerce_naive_utc
    from app.shared_state import runtime_config

    resurface_days = int(
        runtime_config.get(
            "convergence_resurface_after_days", _CONVERGENCE_RESURFACE_AFTER.days
        )
    )
    resurface_after = timedelta(days=resurface_days)

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == LINKED_STATUS,
                    ProjectMessage.message_type.in_(list(BUG_CLASS_TYPES)),
                )
                .order_by(ProjectMessage.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) >= batch_size:
        logger.warning(
            "[REAPER] convergence sweep hit cap=%d linked bug-class messages — "
            "result truncated; next cycle continues",
            batch_size,
        )

    if not msgs:
        return 0

    acted = 0
    pending_broadcasts: list[tuple[str, dict]] = []

    # ── Batch-load linked directions and tasks ───────────────────────
    direction_keys: list[tuple[str, str]] = []
    task_ids: list[uuid.UUID] = []
    for _msg in msgs:
        _link = get_message_link(_msg)
        if not _link:
            continue
        _kind = _link.get("kind")
        _oid = _link.get("id")
        if not _kind or not _oid:
            continue
        if _kind == "direction":
            direction_keys.append((str(_msg.project_id), str(_oid)))
        elif _kind == "task":
            try:
                task_ids.append(uuid.UUID(str(_oid)))
            except (ValueError, AttributeError):
                pass  # handled inside the main loop

    directions_by_key: dict[tuple[str, str], Direction] = {}
    if direction_keys:
        _dir_conditions = [
            and_(
                Direction.project_id == pid,
                Direction.direction_key == dk,
            )
            for pid, dk in direction_keys
        ]
        _dir_rows = (
            (
                await session.execute(
                    select(Direction).where(or_(*_dir_conditions))
                )
            )
            .scalars()
            .all()
        )
        directions_by_key = {
            (str(r.project_id), r.direction_key): r
            for r in _dir_rows
        }

    tasks_by_id: dict[uuid.UUID, Task] = {}
    if task_ids:
        _task_rows = (
            (
                await session.execute(
                    select(Task).where(Task.id.in_(task_ids))
                )
            )
            .scalars()
            .all()
        )
        tasks_by_id = {r.id: r for r in _task_rows}

    for msg in msgs:
        link = get_message_link(msg)
        if not link:
            continue

        kind = link.get("kind")
        outcome_id = link.get("id")
        linked_at_raw = link.get("linked_at")

        if not kind or not outcome_id:
            continue

        linked_at: datetime | None = None
        if linked_at_raw:
            try:
                linked_at = _coerce_naive_utc(
                    datetime.fromisoformat(str(linked_at_raw))
                )
            except (ValueError, TypeError):
                linked_at = None

        if kind == "direction":
            dir_row = directions_by_key.get(
                (str(msg.project_id), str(outcome_id))
            )

            if dir_row is None:
                reason = f"Owning direction '{outcome_id}' no longer exists"
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — direction '%s' missing",
                    msg.id,
                    outcome_id,
                )
                continue

            dir_status = dir_row.status or "active"

            if dir_status == "completed":
                rationale = f"Direction '{outcome_id}' reached completed status"
                _converge_message(msg, now, rationale)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: converged message %s — direction '%s' completed",
                    msg.id,
                    outcome_id,
                )
            elif dir_status in _DIRECTION_FAILED_STATUSES:
                reason = (
                    f"Owning direction '{outcome_id}' ended with status "
                    f"'{dir_status}' without landing"
                )
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — direction '%s' ended '%s'",
                    msg.id,
                    outcome_id,
                    dir_status,
                )
            elif linked_at is not None and (now - linked_at) >= resurface_after:
                reason = (
                    f"Owning direction '{outcome_id}' still in status "
                    f"'{dir_status}' after {(now - linked_at).days} days "
                    f"without delivering the fix"
                )
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — direction '%s' "
                    "still in-flight after %d days",
                    msg.id,
                    outcome_id,
                    (now - linked_at).days,
                )
            # else: in-flight within time bound — leave linked

        elif kind == "task":
            try:
                task_uuid = uuid.UUID(str(outcome_id))
            except (ValueError, AttributeError):
                reason = f"Owning task id '{outcome_id}' is not a valid UUID"
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — task id '%s' "
                    "is not a valid UUID",
                    msg.id,
                    outcome_id,
                )
                continue

            task_row = tasks_by_id.get(task_uuid)

            if task_row is None:
                reason = f"Owning task '{outcome_id}' no longer exists"
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — task '%s' missing",
                    msg.id,
                    outcome_id,
                )
                continue

            task_status = task_row.status

            # CONVERGENCE requires a genuine *landing*, not merely COMPLETED.
            # COMPLETED != merged is a deliberate invariant in this codebase:
            # a fix task can reach COMPLETED with an open, unmerged PR (whose
            # URL canonically lives in the Artifact table, not task.context), so
            # deriving "landed" from a context pr_url heuristic would silently
            # close a live bug report. Reuse the canonical landed detector.
            if is_merged_completion(task_row):
                rationale = f"Owning task '{outcome_id}' completed and PR merged"
                _converge_message(msg, now, rationale)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: converged message %s — task '%s' landed",
                    msg.id,
                    outcome_id,
                )

            elif task_status == TaskStatus.COMPLETED:
                # COMPLETED but not landed (PR open / unmerged) — treat as still
                # in-flight, subject to the time-bound resurfacing below.
                if linked_at is not None and (now - linked_at) >= resurface_after:
                    reason = (
                        f"Owning task '{outcome_id}' completed but did not land "
                        f"(PR not merged) after {(now - linked_at).days} days"
                    )
                    _resurface_message(msg, now, reason)
                    session.add(msg)
                    pending_broadcasts.append(
                        (str(msg.project_id), message_updated_event_payload(msg))
                    )
                    acted += 1
                    logger.info(
                        "[REAPER] Step 15: resurfaced message %s — task '%s' "
                        "completed but did not land after %d days",
                        msg.id,
                        outcome_id,
                        (now - linked_at).days,
                    )
                # else: COMPLETED but not landed yet, within time bound — leave linked

            elif task_status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                status_str = getattr(task_status, "value", str(task_status))
                reason = (
                    f"Owning task '{outcome_id}' ended with status "
                    f"'{status_str}' without landing"
                )
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — task '%s' %s",
                    msg.id,
                    outcome_id,
                    status_str,
                )
            elif linked_at is not None and (now - linked_at) >= resurface_after:
                status_str = getattr(task_status, "value", str(task_status))
                reason = (
                    f"Owning task '{outcome_id}' still in status "
                    f"'{status_str}' after {(now - linked_at).days} days"
                )
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), message_updated_event_payload(msg))
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — task '%s' "
                    "still in-flight after %d days",
                    msg.id,
                    outcome_id,
                    (now - linked_at).days,
                )
            # else: in-flight within time bound — leave linked

    if acted:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] convergence broadcast failed for message %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] Step 15: acted on %d linked bug-class message(s) "
            "(converged or resurfaced)",
            acted,
        )

    return acted


# ── Step 17: auto-resolve OPEN owned bug-class messages on owner success ──────
#
# A BUG_REPORT/BLOCKER created in the 'open' state carries machine-readable
# ownership context stamped at CREATION time (NOT the strategist's
# _convergence_link, which the Step-15 linked sweep owns):
#   - Direction-owned: context['direction_id']  (pause/escalation posters in
#     notification_dispatch.py; the dedup-deadlock BUG_REPORT in
#     research_direction_state.py).
#   - PR/task-owned:    context['pr_number'] + context['source_task_id']
#     (the repeat-fingerprint halt BLOCKER in review_pipeline.py).
#
# Nothing on the integration branch ever resolves these open owned messages:
# the routine-message retention sweep deliberately skips BUG_REPORT/BLOCKER, and
# the Step-15 linked sweep only acts on messages the strategist has explicitly
# moved to the 'linked' state. So once the owner genuinely LANDS — the owning
# direction reaches 'completed', or the owning PR's source task is a merged
# completion — the open message lingers forever in the strategist's inbox as
# noise. This sweep closes only that loop, and ONLY on genuine SUCCESS.
#
# SUCCESS-ONLY, by construction: a message is resolved ONLY when the owning
# direction's status == 'completed' OR the owning PR's source task satisfies the
# canonical landed detector ``landed_signal.is_merged_completion`` (COMPLETED AND
# carries the integration-branch landed signal). An owner that ended WITHOUT
# delivering the fix — a retired/rejected/expired/superseded direction; a
# failed/cancelled/missing task; a COMPLETED-but-unmerged task; an unknown,
# malformed, or still-in-flight owner — leaves the message OPEN. Reaching some
# terminal status is NOT enough; only landing resolves.
#
# Fail-safe: per-message owner resolution is wrapped so one bad row can never
# abort the batch, and any error or ambiguity leaves the message open.
#
# Idempotent: resolution moves status off 'open', so the next pass (which filters
# on status == 'open') skips already-resolved rows — re-running is a no-op.

# Context audit keys stamped on an auto-resolution (traceability + idempotency).
_OWNED_AUTO_RESOLVED_AT_KEY = "_owned_auto_resolved_at"
_OWNED_AUTO_RESOLUTION_KEY = "_owned_auto_resolution"


async def _owned_message_resolution(session, msg, *, verify_pr_merged) -> str | None:
    """High-confidence audit string when ``msg``'s owner genuinely LANDED, else None.

    Reads ownership context stamped at message creation (``direction_id`` first,
    then ``pr_number`` + ``source_task_id``). Direction ownership wins when both
    are present. Returns ``None`` — fail-safe, leaving the message open — for any
    missing/unknown/malformed/still-in-flight owner or any owner that ended
    without landing the fix.
    """
    from app.landed_signal import is_merged_completion
    from app.models import Direction

    ctx = msg.context if isinstance(msg.context, dict) else {}

    # 1) Direction-owned: resolve ONLY on status == 'completed'. A direction that
    #    was retired/rejected/expired/superseded ended WITHOUT landing the fix and
    #    must leave the message open.
    direction_id = ctx.get("direction_id")
    if direction_id:
        dir_row = (
            (
                await session.execute(
                    select(Direction).where(
                        Direction.project_id == msg.project_id,
                        Direction.direction_key == str(direction_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        if dir_row is not None and (dir_row.status or "active") == "completed":
            return f"Auto-resolved: owning direction '{direction_id}' completed"
        # Direction owner present but not completed (or missing) — fail-safe open.
        # Fall through only when there is NO direction owner at all.
        if dir_row is not None:
            return None

    # 2) PR/task-owned: resolve ONLY when the source task is a merged completion.
    #    Never infer 'merged' from a pr_url heuristic; never treat a
    #    COMPLETED-but-unmerged task as success.
    pr_number = ctx.get("pr_number")
    source_task_id = ctx.get("source_task_id")
    if pr_number is None or not source_task_id:
        return None
    try:
        task_uuid = uuid.UUID(str(source_task_id))
    except (ValueError, AttributeError, TypeError):
        return None
    task = await session.get(Task, task_uuid)
    if task is None:
        return None
    # Primary detector: context-based, side-effect-free, identical to the Step-15
    # linked sweep ("COMPLETED AND carries the landed signal"). A merged PR's
    # source task is stamped landed at merge time.
    if is_merged_completion(task):
        return f"Auto-resolved: owning PR #{pr_number} merged"
    # Secondary confirmation via the GitHub-backed verifier ONLY for a task that
    # is already COMPLETED (so a still-active task is never probed and a
    # COMPLETED-but-unmerged task is left open unless the verifier confirms the
    # merge). Any verifier error is fail-safe (leaves the message open).
    if task.status == TaskStatus.COMPLETED and verify_pr_merged is not None:
        try:
            if await verify_pr_merged(task, session):
                return f"Auto-resolved: owning PR #{pr_number} merged"
        except Exception:
            return None
    return None


async def sweep_resolve_open_owned_bug_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
    verify_pr_merged=None,
) -> int:
    """Step 17: auto-resolve OPEN owned BUG_REPORT/BLOCKER messages on owner success.

    Selects up to ``batch_size`` OPEN, system/agent-authored bug-class messages
    (DISJOINT from the Step-15 'linked' sweep) and resolves each one whose owning
    outcome — read from creation-time context — has genuinely landed: the owning
    direction reached 'completed', or the owning PR's source task is a merged
    completion. Everything else (owner ended without landing, COMPLETED-but-
    unmerged, missing/unknown/malformed/still-active, or any lookup error) leaves
    the message OPEN.

    Returns the number of messages auto-resolved.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import ProjectMessage
    from app.message_convergence import (
        BUG_CLASS_TYPES,
        OPEN_STATUS,
        SYSTEM_AUTHORED_FROM_TYPES,
    )
    from app.project_message_projection import message_updated_event_payload

    if verify_pr_merged is None:
        from app.rollup_pr_verification import _verify_pr_merged

        verify_pr_merged = _verify_pr_merged

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type.in_(list(SYSTEM_AUTHORED_FROM_TYPES)),
                    ProjectMessage.message_type.in_(list(BUG_CLASS_TYPES)),
                )
                .order_by(ProjectMessage.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) >= batch_size:
        logger.warning(
            "[REAPER] open-owned-message sweep hit cap=%d open bug-class messages — "
            "result truncated; next cycle continues",
            batch_size,
        )

    if not msgs:
        return 0

    resolved = 0
    pending_broadcasts: list[tuple[str, dict]] = []
    for msg in msgs:
        # Fail-safe: one malformed/unreadable owner must never abort the batch.
        try:
            audit = await _owned_message_resolution(
                session, msg, verify_pr_merged=verify_pr_merged
            )
        except Exception:
            logger.warning(
                "[REAPER] Step 17: owner resolution failed for message %s — "
                "leaving open",
                msg.id,
                exc_info=True,
            )
            continue
        if audit is None:
            continue
        msg.status = "resolved"
        msg.resolution = audit
        msg.resolved_at = now
        ctx = dict(msg.context or {})
        ctx[_OWNED_AUTO_RESOLVED_AT_KEY] = now.isoformat()
        ctx[_OWNED_AUTO_RESOLUTION_KEY] = audit
        msg.context = ctx
        session.add(msg)
        resolved += 1
        pending_broadcasts.append(
            (str(msg.project_id), message_updated_event_payload(msg))
        )
        logger.info(
            "[REAPER] Step 17: auto-resolved owned message %s — %s",
            msg.id,
            audit,
        )

    if resolved:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] Step 17: broadcast failed for message %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] Step 17: auto-resolved %d open owned bug-class message(s)",
            resolved,
        )

    return resolved


# ── Step 18: negatively resolve OPEN bug-class messages confirmed stale on HEAD ─
#
# Step 17 (above) is the POSITIVE half: it resolves an open owned bug-class
# message when its owning outcome genuinely LANDS. This step is the symmetric
# NEGATIVE half: it resolves an open bug-class message whose cited code is
# unambiguously ABSENT on the CURRENT integration HEAD — i.e. the defect no
# longer reproduces — even though nothing "owns" or landed a fix for it.
#
# Until this sweep, such a message was only deprioritised in the strategist
# inbox and tagged with a STALE marker, relying entirely on the strategist LLM
# to remember to close it; until it did, the message stayed open, was re-fetched
# every strategic cycle, and re-consumed scarce inbox-attention budget. This is
# the message-lifecycle equivalent of the reproduce-on-HEAD gate the
# research-finding ingestion path already enforces.
#
# CONSERVATIVE, by construction:
#   - Citations are re-grounded FRESH against the current HEAD at sweep time via
#     ``compute_message_citation_warning`` — the ingestion-time provenance stamp
#     is NEVER trusted (the code may have been re-added since).
#   - That helper honours a tri-state and may also surface mixed non-absent
#     provenance (broken/ungrounded refs on an otherwise actionable report), so
#     this sweep MUST gate explicitly on ``citation_state == 'absent'``. Any
#     ``present``/``indeterminate`` verdict leaves the message OPEN.
#
# Ordering: this runs AFTER Step 17 in the reaper cycle, so an owner that
# genuinely landed is positively resolved first (the better, more specific
# reason) before this negative sweep ever sees it. The two never race because
# both filter on ``status == 'open'`` and Step 17 commits before Step 18 runs.
#
# Fail-safe: per-message grounding is wrapped so one bad row (or one grounding
# error) can never abort the batch, and any error/indeterminate verdict leaves
# the message OPEN. Idempotent: resolution moves status off 'open', so the next
# pass skips it. Reversible: an operator reopening the message (status back to
# 'open') makes it eligible again, exactly like the Step-17 path.

# Context audit keys stamped on a stale auto-resolution — DISTINCT from the
# Step-17 owner-landed keys so the two resolution kinds are distinguishable in
# audit. The stored citation signal records what was absent on HEAD.
_STALE_AUTO_RESOLVED_AT_KEY = "_stale_auto_resolved_at"
_STALE_AUTO_RESOLUTION_KEY = "_stale_auto_resolution"
_STALE_AUTO_CITATION_SIGNAL_KEY = "_stale_auto_citation_signal"


async def _stale_bug_message_resolution(
    session, msg, project_cache
) -> tuple[str, dict] | None:
    """`(audit, signal)` when ``msg``'s citations are absent on HEAD, else None.

    Re-grounds the message's citations FRESH against the current integration
    HEAD via :func:`compute_message_citation_warning`. That helper may return a
    mixed non-absent provenance signal, so this sweep trusts it ONLY when
    ``citation_state == "absent"``. The ingestion-time provenance stamp is
    never consulted. Returns None — fail-safe, leaving the message open — for
    any present, indeterminate, or partially-grounded verdict.

    ``project_cache`` memoises ``Project`` lookups across the batch by project_id.
    """
    from app.models import Project
    from app.source_ref_validation import compute_message_citation_warning

    pid = msg.project_id
    if pid not in project_cache:
        project_cache[pid] = await session.get(Project, pid)
    project = project_cache[pid]
    if project is None:
        return None

    signal = await compute_message_citation_warning(
        session,
        project,
        msg.content,
        structured_context=msg.context if isinstance(msg.context, dict) else None,
    )
    if not signal or signal.get("citation_state") != "absent":
        # present / indeterminate / partially-grounded — never resolve.
        return None
    audit = (
        "Auto-resolved as stale: cited code is absent on the current integration "
        "HEAD and the defect no longer reproduces"
    )
    return audit, signal


async def sweep_resolve_stale_open_bug_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
) -> int:
    """Step 18: negatively resolve OPEN bug-class messages confirmed stale on HEAD.

    Selects the SAME OPEN, system/agent-authored bug-class set as Step 17 and
    resolves each message whose citations re-validate as unambiguously ABSENT
    against the current integration HEAD (re-checked fresh, never trusting the
    ingestion-time stamp). A present, indeterminate, or partially-grounded
    verdict — or any grounding error — leaves the message OPEN. Disjoint in
    effect from Step 17 (which runs first and positively resolves landed owners).

    Returns the number of messages auto-resolved as stale.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import ProjectMessage
    from app.message_convergence import (
        BUG_CLASS_TYPES,
        OPEN_STATUS,
        SYSTEM_AUTHORED_FROM_TYPES,
    )
    from app.project_message_projection import message_updated_event_payload

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type.in_(list(SYSTEM_AUTHORED_FROM_TYPES)),
                    ProjectMessage.message_type.in_(list(BUG_CLASS_TYPES)),
                )
                .order_by(ProjectMessage.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) >= batch_size:
        logger.warning(
            "[REAPER] stale-bug-message sweep hit cap=%d open bug-class messages — "
            "result truncated; next cycle continues",
            batch_size,
        )

    if not msgs:
        return 0

    resolved = 0
    project_cache: dict = {}
    pending_broadcasts: list[tuple[str, dict]] = []
    for msg in msgs:
        # Fail-safe: one malformed row or one grounding error must never abort
        # the batch, and any error leaves the message open.
        try:
            outcome = await _stale_bug_message_resolution(session, msg, project_cache)
        except Exception:
            logger.warning(
                "[REAPER] Step 18: stale grounding failed for message %s — "
                "leaving open",
                msg.id,
                exc_info=True,
            )
            continue
        if outcome is None:
            continue
        audit, signal = outcome
        msg.status = "resolved"
        msg.resolution = audit
        msg.resolved_at = now
        ctx = dict(msg.context or {})
        ctx[_STALE_AUTO_RESOLVED_AT_KEY] = now.isoformat()
        ctx[_STALE_AUTO_RESOLUTION_KEY] = audit
        ctx[_STALE_AUTO_CITATION_SIGNAL_KEY] = signal
        msg.context = ctx
        session.add(msg)
        resolved += 1
        pending_broadcasts.append(
            (str(msg.project_id), message_updated_event_payload(msg))
        )
        logger.info(
            "[REAPER] Step 18: auto-resolved stale message %s — %s",
            msg.id,
            audit,
        )

    if resolved:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] Step 18: broadcast failed for message %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] Step 18: auto-resolved %d stale open bug-class message(s)",
            resolved,
        )

    return resolved


# ── Wake-delivery sweep observability ────────────────────────────────────────
#
# The wake-delivery sweep historically returned a bare int (wakes delivered) and
# the reaper discarded it — so a wedged-but-not-crashed delivery path left no
# per-cycle signal. ``WakeSweepResult`` carries the full per-pass breakdown the
# reaper now logs, WITHOUT changing the contract: it subclasses ``int`` and its
# integer value IS ``delivered``, so every existing caller / test that compares
# the return to an int keeps working unchanged.

# Consecutive reaper cycles where the sweep delivered nothing while FIRED-but-
# undelivered registrations remained. A genuinely-progressing or idle sweep
# resets this to 0; only a stuck delivery path lets it climb. Module-level so it
# persists across the leader's reaper cycles (the sweep is otherwise stateless).
_wake_delivery_stall_cycles: int = 0
# Consecutive no-progress cycles (with a non-empty FIRED backlog) after which the
# cross-cycle delivery-stall signal trips. One cycle of no progress can be benign
# (a registration fired mid-sweep); a sustained run is the wedged-sweep signature.
_WAKE_DELIVERY_STALL_CYCLE_THRESHOLD: int = 3


def reset_wake_delivery_stall_state() -> None:
    """Reset the cross-cycle delivery-stall counter (test hook / leader handoff)."""
    global _wake_delivery_stall_cycles
    _wake_delivery_stall_cycles = 0


def get_wake_delivery_stall_cycles() -> int:
    """Return the current value of the cross-cycle delivery-stall counter."""
    return _wake_delivery_stall_cycles


class WakeSweepResult(int):
    """Per-cycle outcome of :func:`sweep_deliver_task_wakes`.

    Subclasses ``int`` so the historical contract is preserved exactly — the
    integer value equals ``delivered`` (pass-1 wakes), and existing callers/tests
    that do ``assert result == 1`` keep working — while the reaper reads the
    richer breakdown for per-cycle logging:

      * ``delivered``         — PARKED tasks advanced this cycle (pass 1)
      * ``expired``           — PENDING registrations aged out at deadline (pass 2)
      * ``stranded``          — PARKED tasks failed with no live registration (pass 3)
      * ``delivery_failed``   — FIRED registrations marked FAILED after a delivery
                                error (poison rows)
      * ``fired_undelivered`` — FIRED registrations still undelivered after the
                                sweep (the residual delivery backlog)
      * ``stall_cycles``      — consecutive no-progress cycles with a FIRED backlog
      * ``delivery_stalled``  — True once ``stall_cycles`` crosses the threshold
      * ``post_commit_stuck`` — DELIVERED rows that still returned False from
                                ``run_compute_wake_post_commit`` this cycle
    """

    delivered: int
    expired: int
    stranded: int
    delivery_failed: int
    fired_undelivered: int
    stall_cycles: int
    delivery_stalled: bool
    post_commit_stuck: int

    def __new__(
        cls,
        delivered: int,
        *,
        expired: int = 0,
        stranded: int = 0,
        delivery_failed: int = 0,
        fired_undelivered: int = 0,
        stall_cycles: int = 0,
        delivery_stalled: bool = False,
        post_commit_stuck: int = 0,
    ) -> "WakeSweepResult":
        self = super().__new__(cls, delivered)
        self.delivered = int(delivered)
        self.expired = int(expired)
        self.stranded = int(stranded)
        self.delivery_failed = int(delivery_failed)
        self.fired_undelivered = int(fired_undelivered)
        self.stall_cycles = int(stall_cycles)
        self.delivery_stalled = bool(delivery_stalled)
        self.post_commit_stuck = int(post_commit_stuck)
        return self

    def as_log_fields(self) -> dict:
        """Flat dict of the per-pass counts for structured per-cycle logging."""
        return {
            "delivered": self.delivered,
            "expired": self.expired,
            "stranded": self.stranded,
            "delivery_failed": self.delivery_failed,
            "fired_undelivered": self.fired_undelivered,
            "stall_cycles": self.stall_cycles,
            "delivery_stalled": self.delivery_stalled,
            "post_commit_stuck": self.post_commit_stuck,
        }

    def is_noteworthy(self) -> bool:
        """True when the cycle did anything (or is stalled) worth a log line."""
        return bool(
            self.delivered
            or self.expired
            or self.stranded
            or self.delivery_failed
            or self.delivery_stalled
            or self.post_commit_stuck
        )


async def sweep_deliver_task_wakes(
    session, now: datetime, *, batch_size: int = 200
) -> "WakeSweepResult":
    """Leader-owned wake-delivery worker for the task park-and-wake primitive.

    Three passes, each claiming rows ``FOR UPDATE SKIP LOCKED`` so two sweeps (or
    a sweep racing the external firing side) never double-process a registration:

    1. **Deliver** FIRED registrations — advance each PARKED task *exactly once*
       via ``task_lifecycle.deliver_wake``, **one registration per transaction**.
    2. **Expire** PENDING registrations whose ``deadline_at`` passed before the
       source fired — age the task out (FAILED) rather than strand it.
    3. **Strand-guard** PARKED tasks older than the strand threshold that have no
       remaining PENDING/FIRED registration (e.g. their registration row was lost)
       — fail them so a parked task can never sit forever with nothing to wake it.

    Returns a :class:`WakeSweepResult` — an ``int`` (== wakes delivered, the
    historical contract) that ALSO carries the per-pass breakdown (expired,
    stranded, delivery-failed, residual FIRED backlog) and the cross-cycle
    delivery-stall signal, so the reaper can log per-cycle wake health.
    Idempotent and safe to run every reaper cycle.

    Pass 1 commits per-registration (not once per batch). ``deliver_wake`` is
    caller-committed for EVERY branch — including the compute-job FINALIZE branch,
    which used to commit internally — so the FIRED→DELIVERED flip and the task
    advance commit together atomically, exactly once. Per-registration commits
    guarantee that (a) one registration's failure can never roll back
    already-delivered siblings, and (b) committing one delivery never releases the
    ``FOR UPDATE`` lock of a registration not yet processed, because each delivery
    is the only row locked in its own transaction. Compute finalizations also
    commit internal KnowledgeDocument rows; their post-commit broadcast + knowledge
    persistence run via ``run_compute_wake_post_commit`` AFTER the delivery commit.
    """
    from sqlalchemy import func, or_

    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND
    from app.database import resolve_dialect_name
    from app.failure_classification import PARK_WAKE_STRANDED
    from app.task_completion import (
        _COMPUTE_WAKE_NEXT_RETRY_KEY,
        _COMPUTE_WAKE_POST_COMMIT_META_KEY,
        run_compute_wake_post_commit,
    )
    from app.task_lifecycle import (
        _PARK_STRAND_SECONDS,
        deliver_wake,
        expire_wake,
    )

    delivered = 0
    expired = 0
    stranded_count = 0
    delivery_failed = 0
    post_commit_stuck = 0
    touched = False
    now_iso = now.isoformat()

    # Retry delivered compute-wake registrations that are carrying the durable
    # post-commit marker before processing any new FIRED rows. This keeps the
    # wake side effects recoverable even after the delivery row itself has been
    # committed as DELIVERED.
    #
    # ``has_key`` uses the JSONB ``?`` operator (PostgreSQL only). On SQLite
    # (standalone tests) these predicates are omitted; the Python loop guards
    # below still filter correctly in that case.
    #
    # The ``_COMPUTE_WAKE_NEXT_RETRY_KEY`` filter excludes rows whose backoff
    # window has not yet elapsed, preventing a failing batch from starving
    # newer ready rows: a row is eligible only if the key is absent (never
    # failed) OR its stored timestamp is <= now.
    _retry_where = [
        TaskWakeRegistration.delivery_state == WakeDeliveryState.DELIVERED,
        TaskWakeRegistration.wake_source_kind == COMPUTE_JOB_TERMINAL_WAKE_KIND,
    ]
    if resolve_dialect_name(session) != "sqlite":
        _retry_where.append(
            TaskWakeRegistration.source_metadata.has_key(
                _COMPUTE_WAKE_POST_COMMIT_META_KEY
            )
        )
        _retry_where.append(
            or_(
                ~TaskWakeRegistration.source_metadata.has_key(
                    _COMPUTE_WAKE_NEXT_RETRY_KEY
                ),
                TaskWakeRegistration.source_metadata[
                    _COMPUTE_WAKE_NEXT_RETRY_KEY
                ].astext
                <= now_iso,
            )
        )
    retry_rows = (
        (
            await session.execute(
                select(TaskWakeRegistration)
                .where(*_retry_where)
                .order_by(TaskWakeRegistration.delivered_at.asc().nullsfirst())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for reg in retry_rows:
        if _COMPUTE_WAKE_POST_COMMIT_META_KEY not in (reg.source_metadata or {}):
            continue
        # SQLite guard: skip rows whose backoff window has not expired yet.
        if (reg.source_metadata or {}).get(_COMPUTE_WAKE_NEXT_RETRY_KEY, "") > now_iso:
            continue
        try:
            ok = await run_compute_wake_post_commit(session, reg)
            if not ok:
                post_commit_stuck += 1
        except Exception:
            post_commit_stuck += 1
            logger.exception(
                "[REAPER] Wake post-commit retry failed for registration %s",
                reg.id,
            )

    # Pass 1: deliver fired registrations, one registration per transaction. Each
    # FIRED row is claimed (FOR UPDATE SKIP LOCKED, LIMIT 1), delivered, and
    # committed in isolation so a commit never frees a sibling's lock and a single
    # failure never rolls back already-delivered siblings.
    for _ in range(batch_size):
        reg = (
            (
                await session.execute(
                    select(TaskWakeRegistration)
                    .where(
                        TaskWakeRegistration.delivery_state == WakeDeliveryState.FIRED
                    )
                    .order_by(TaskWakeRegistration.fired_at.asc().nullsfirst())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .first()
        )
        if reg is None:
            break
        reg_id = reg.id
        try:
            advanced = await deliver_wake(session, reg, now=now)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "[REAPER] Wake delivery failed for registration %s", reg_id
            )
            # A poison registration must not stall the whole pass (LIMIT-1 re-select
            # would loop on it forever): re-fetch it fresh and mark it FAILED so the
            # next iteration moves past it. Best-effort — never re-raises.
            with contextlib.suppress(Exception):
                poisoned = await session.get(TaskWakeRegistration, reg_id)
                if (
                    poisoned is not None
                    and poisoned.delivery_state == WakeDeliveryState.FIRED
                ):
                    poisoned.delivery_state = WakeDeliveryState.FAILED
                    poisoned.result_state = "delivery_error"
                    poisoned.delivered_at = now
                    session.add(poisoned)
                    await session.commit()
                    delivery_failed += 1
                else:
                    await session.rollback()
            continue
        if advanced:
            delivered += 1
            # Post-commit, best-effort: operator broadcast + durable knowledge for
            # a wake-finalized compute experiment. Deferred until AFTER the delivery
            # commit so the KnowledgeDocument's own internal commit cannot release
            # the delivery row lock mid-pass. No-op for non-compute wake kinds.
            await run_compute_wake_post_commit(session, reg)

    # Pass 2: expire due, never-fired registrations.
    due = (
        (
            await session.execute(
                select(TaskWakeRegistration)
                .where(
                    TaskWakeRegistration.delivery_state == WakeDeliveryState.PENDING,
                    TaskWakeRegistration.deadline_at.is_not(None),
                    TaskWakeRegistration.deadline_at <= now,
                )
                .order_by(TaskWakeRegistration.deadline_at.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for reg in due:
        touched = True
        if await expire_wake(session, reg, now=now):
            expired += 1

    # Pass 3: strand-guard. A PARKED task whose parked_at is older than the strand
    # threshold and which has no live (PENDING/FIRED) registration cannot be woken
    # by anything — fail it rather than leave it stranded. Uses ix_task_parked_at.
    strand_cutoff = now - timedelta(seconds=_PARK_STRAND_SECONDS)
    stranded = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.status == TaskStatus.PARKED,
                    Task.parked_at.is_not(None),
                    Task.parked_at <= strand_cutoff,
                )
                .order_by(Task.parked_at.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    task_ids = [task.id for task in stranded]
    live_task_ids: set = set()
    if task_ids:
        live_rows = (
            await session.execute(
                select(TaskWakeRegistration.task_id)
                .where(
                    TaskWakeRegistration.task_id.in_(task_ids),
                    TaskWakeRegistration.delivery_state.in_(
                        [WakeDeliveryState.PENDING, WakeDeliveryState.FIRED]
                    ),
                )
                .distinct()
            )
        ).scalars().all()
        live_task_ids = set(live_rows)
    for task in stranded:
        if task.id in live_task_ids:
            continue
        touched = True
        stranded_count += 1
        task.status = TaskStatus.FAILED
        task.parked_at = None
        # Clear park_reason too so a stranded task carries no stale "Parked …"
        # marker on operator surfaces — the task is now FAILED, not parked.
        task.park_reason = None
        task.latest_status_message = (
            "Parked task stranded with no live wake registration; failing"
        )[:500]
        ctx = dict(task.context or {})
        # PARK_WAKE_STRANDED is a canonical recognized failure class (budget-exempt
        # INFRA, human-labelled) so this terminal outcome no longer presents as a
        # generic code-like residual fingerprint in failure distributions.
        ctx["failure_category"] = PARK_WAKE_STRANDED
        ctx["failure_class"] = PARK_WAKE_STRANDED
        task.context = ctx
        with contextlib.suppress(Exception):
            from app.jwt_revocation import emit_task_terminated_notify

            emit_task_terminated_notify(session, str(task.id))
        session.add(task)

    if touched:
        await session.commit()

    # Residual delivery backlog: FIRED registrations still undelivered after the
    # sweep (claimed by another worker, beyond batch_size, or fired mid-sweep). A
    # healthy sweep drains this within a cycle; a persistent backlog with no
    # delivery progress is the wedged-sweep signature.
    fired_filters = [TaskWakeRegistration.delivery_state == WakeDeliveryState.FIRED]
    fired_undelivered = (
        await session.execute(
            select(func.count()).select_from(TaskWakeRegistration).where(*fired_filters)
        )
    ).scalar() or 0

    # Cross-cycle delivery-stall signal: a non-empty FIRED backlog that the sweep
    # made NO progress against this cycle climbs the counter; any progress (or an
    # empty backlog) resets it. The signal trips once it sustains across the
    # threshold so a single benign mid-sweep fire does not false-alarm.
    global _wake_delivery_stall_cycles
    if fired_undelivered > 0 and delivered == 0:
        _wake_delivery_stall_cycles += 1
    else:
        _wake_delivery_stall_cycles = 0
    delivery_stalled = (
        _wake_delivery_stall_cycles >= _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD
    )

    return WakeSweepResult(
        delivered,
        expired=expired,
        stranded=stranded_count,
        delivery_failed=delivery_failed,
        fired_undelivered=int(fired_undelivered),
        stall_cycles=_wake_delivery_stall_cycles,
        delivery_stalled=delivery_stalled,
        post_commit_stuck=post_commit_stuck,
    )


# ── Step 18d: age-based decay for unowned OPEN BUG_REPORT / BLOCKER messages ─
#
# Steps 15, 17, and 18 resolve bug-class messages when an owning outcome lands
# or when cited code is absent on HEAD.  None of those paths handles a
# BUG_REPORT or BLOCKER that was never owned, never linked to a live outcome,
# and whose cited code cannot be definitively grounded either way.  Such
# messages currently stay OPEN forever — noise in the operator inbox that the
# strategist must manually dismiss.
#
# This sweep adds a standalone age-based fallback: after STALE_OPEN_BUG_CLASS_DECAY_DAYS
# (default 30) an unowned, unlinked, system/agent-authored BUG_REPORT or BLOCKER
# is auto-decayed just like a stale conversational NOTE.  Operator-authored
# messages ("user") are excluded — only machine-generated findings decay
# automatically.  Messages still carrying a live ``_convergence_link`` are
# skipped so the Step-15 convergence sweep owns the terminal transition.
#
# Conservative by construction: messages authored by operators stay open
# indefinitely; the threshold is long enough (30 days, overridable via
# ``runtime_config['stale_open_bug_class_decay_days']``) that genuinely
# actionable findings are unlikely to have been missed; and the sweep only
# acts on the OPEN state, so a linked or resolved message is never touched.

# Default decay window for unowned OPEN BUG_REPORT / BLOCKER messages.
STALE_OPEN_BUG_CLASS_DECAY_DAYS = 30


async def sweep_decay_stale_open_bug_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
) -> int:
    """Step 18d: age-out unowned OPEN BUG_REPORT/BLOCKER messages.

    Selects up to ``batch_size`` OPEN, system/agent-authored BUG_REPORT or
    BLOCKER messages with no live ``_convergence_link``, no creation-time
    ownership context (``direction_id`` or ``pr_number`` + ``source_task_id``),
    and ``created_at`` older than ``STALE_OPEN_BUG_CLASS_DECAY_DAYS``
    (configurable via ``runtime_config['stale_open_bug_class_decay_days']``).
    Resolves each with ``resolution='auto-decayed (stale)'``.

    Disjoint in effect from Step 17 (only acts on messages that have no
    owning context) and from Step 15 (the linked sweep owns messages with a
    live ``_convergence_link``). Fail-safe per message, idempotent (resolution
    moves status off 'open'), and broadcasts a ``message_updated`` envelope
    per resolved row.

    Returns the number of messages auto-decayed.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import ProjectMessage
    from app.message_convergence import (
        BUG_CLASS_TYPES,
        OPEN_STATUS,
        SYSTEM_AUTHORED_FROM_TYPES,
        get_message_link,
        mark_message_resolved,
    )
    from app.project_message_projection import message_updated_event_payload
    from app.shared_state import runtime_config

    decay_days = int(
        runtime_config.get(
            "stale_open_bug_class_decay_days", STALE_OPEN_BUG_CLASS_DECAY_DAYS
        )
    )
    cutoff = now - timedelta(days=decay_days)

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type.in_(list(SYSTEM_AUTHORED_FROM_TYPES)),
                    ProjectMessage.message_type.in_(list(BUG_CLASS_TYPES)),
                    ProjectMessage.created_at <= cutoff,
                )
                .order_by(ProjectMessage.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) >= batch_size:
        logger.warning(
            "[REAPER] stale-open-bug decay sweep hit cap=%d open bug-class "
            "messages — result truncated; next cycle continues",
            batch_size,
        )
    if not msgs:
        return 0

    resolved = 0
    pending_broadcasts: list[tuple[str, dict]] = []
    for msg in msgs:
        # Skip messages that still carry a live convergence link — the Step-15
        # sweep owns their terminal transition.
        if get_message_link(msg) is not None:
            continue
        # Skip messages that carry creation-time ownership context — Step 17
        # owns their lifecycle (resolves on owner success, NOT on age). A
        # message with a direction_id or a pr_number+source_task_id is
        # intentionally kept open by Step 17 when its owner failed, is still
        # in-flight, or cannot be confirmed landed; decaying it here would
        # silently clear an actionable BLOCKER/BUG_REPORT.
        ctx = msg.context if isinstance(msg.context, dict) else {}
        if ctx.get("direction_id") or (
            ctx.get("pr_number") and ctx.get("source_task_id")
        ):
            continue
        mark_message_resolved(msg, now, "auto-decayed (stale)")
        session.add(msg)
        resolved += 1
        pending_broadcasts.append(
            (str(msg.project_id), message_updated_event_payload(msg))
        )
        logger.info(
            "[REAPER] Step 18d: auto-decayed stale bug-class message %s "
            "(no activity for >=%dd)",
            msg.id,
            decay_days,
        )

    if resolved:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] Step 18d: broadcast failed for message %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] Step 18d: auto-decayed %d stale open bug-class message(s)",
            resolved,
        )
    return resolved


# ── Step 18e: age-based decay for unlinked OPEN BLOCKER messages (all authors) ─
#
# Step 18d decays system/agent-authored BUG_REPORT and BLOCKER messages that
# have no owning context after 30 days.  Operator-authored ("user") BLOCKERs
# are deliberately excluded from that sweep — an operator who files a BLOCKER
# is not a machine-generated finding and should not be auto-decayed on the
# short window.
#
# However, a user-authored BLOCKER with no convergence link, no direction_id,
# and no pr_number+source_task_id is a dead thread: there is no automated
# mechanism to resolve it, and it persists in the operator inbox forever.
# This sweep closes that gap with a conservative 60-day threshold (default,
# configurable via runtime_config['stale_open_unlinked_blocker_decay_days']).
#
# Disjoint from Step 18d (which only handles system/agent authors), from
# Step 17 (which requires ownership context), and from Step 15 (which requires
# a live _convergence_link in status == 'linked').  Like Step 18d, owned or
# linked messages are skipped so the appropriate sweep owns the terminal
# transition.

# Default decay window for unlinked OPEN BLOCKER messages (all authors).
STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS = 60


async def sweep_decay_stale_unlinked_blocker_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
) -> int:
    """Step 18e: age-out unlinked OPEN BLOCKER messages (all authors).

    Selects up to ``batch_size`` OPEN BLOCKER messages (any ``from_type``,
    including "user") with no live ``_convergence_link``, no creation-time
    ownership context (``direction_id`` or ``pr_number`` + ``source_task_id``),
    and ``created_at`` older than ``STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS``
    (configurable via ``runtime_config['stale_open_unlinked_blocker_decay_days']``).
    Resolves each with ``resolution='auto-decayed (unlinked dead thread)'``.

    This is the counterpart to Step 18d: where 18d restricts to system/agent
    authors and a 30-day threshold, this sweep catches user-authored dead
    threads at a longer (60-day) conservative threshold.

    Disjoint from Step 17 (ownership context present) and Step 15 (linked
    status). Fail-safe per message, idempotent (resolution moves status off
    'open'), and broadcasts a ``message_updated`` envelope per resolved row.

    Returns the number of messages auto-decayed.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import MessageType, ProjectMessage
    from app.message_convergence import (
        OPEN_STATUS,
        get_message_link,
        mark_message_resolved,
    )
    from app.project_message_projection import message_updated_event_payload
    from app.shared_state import runtime_config

    decay_days = int(
        runtime_config.get(
            "stale_open_unlinked_blocker_decay_days",
            STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS,
        )
    )
    cutoff = now - timedelta(days=decay_days)

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.message_type == MessageType.BLOCKER.value,
                    ProjectMessage.created_at <= cutoff,
                )
                .order_by(ProjectMessage.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) >= batch_size:
        logger.warning(
            "[REAPER] stale-unlinked-blocker decay sweep hit cap=%d open BLOCKER "
            "messages — result truncated; next cycle continues",
            batch_size,
        )
    if not msgs:
        return 0

    resolved = 0
    pending_broadcasts: list[tuple[str, dict]] = []
    for msg in msgs:
        # Skip messages that still carry a live convergence link — Step 15 owns them.
        if get_message_link(msg) is not None:
            continue
        # Skip messages with creation-time ownership context — Step 17 owns them.
        ctx = msg.context if isinstance(msg.context, dict) else {}
        if ctx.get("direction_id") or (
            ctx.get("pr_number") and ctx.get("source_task_id")
        ):
            continue
        mark_message_resolved(msg, now, "auto-decayed (unlinked dead thread)")
        session.add(msg)
        resolved += 1
        pending_broadcasts.append(
            (str(msg.project_id), message_updated_event_payload(msg))
        )
        logger.info(
            "[REAPER] Step 18e: auto-decayed unlinked BLOCKER %s "
            "(no activity for >=%dd, from_type=%s)",
            msg.id,
            decay_days,
            msg.from_type,
        )

    if resolved:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] Step 18e: broadcast failed for message %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] Step 18e: auto-decayed %d unlinked open BLOCKER message(s)",
            resolved,
        )
    return resolved


# ── Step 25: age-based failure sweep for stranded PENDING conversation sends ─
#
# An operator chat message is accepted and persisted (surface='conversation',
# handling_state='pending') before any runner claim is established. If the
# selected runner is dead, no claim ever lands: the message sits at
# handling_state='pending' forever with no automated mechanism to notice.
# From the operator's perspective this is indistinguishable from a reply
# still in flight — there is no failed state and no sweep that reclaims it.
#
# This sweep closes that gap: an unclaimed PENDING **operator-authored**
# conversation message (from_type='user', conversation_claim_id IS NULL) that
# has been sitting in its current unclaimed state past the bounded window is
# transitioned to handling_state='failed' / status='resolved', a terminal,
# operator-visible outcome distinct from both 'pending' (in flight) and a
# genuine 'responded' resolution. A message that IS claimed is owned by
# conversation_coordinator's lease-expiry/reclaim path, not this sweep — a
# live or expired-but-claimed lease can still be recovered by a replacement
# generation.
#
# Three shapes look identical to a naive predicate but have a designed answer
# path that runs LATER, so they are deliberately outside this sweep:
#
#   1. System-authored Workday consultation packets and consultation result
#      rows (conversation_coordinator.build/record paths) are also
#      surface=CONVERSATION / PENDING / status='open' with
#      conversation_claim_id permanently NULL — their claim lives in
#      context['_workday_consultation_claim_id']. Failing one makes it
#      unclaimable (the claim gates require status=='open') and zeroes the
#      operator-visible consultation phase. Excluded by from_type=='user'
#      plus an explicit consultation-marker guard.
#   2. Rows deferred by the non-continuous (Chat-mode) path. No runner is
#      spawned on send; the cadence strategist answers them up to
#      ``strategic_max_interval_minutes`` (default 1440) later, and the row is
#      stamped wakeup_state 'paused' / 'waiting_for_capacity' to model exactly
#      that. Rows released by session handoff (context carries
#      ``_resident_handoff_at``) are likewise waiting for a *next* session to
#      rebind them. Both get the much longer deferred window below rather than
#      the armed-runner window.
#   3. The synthetic resident-activation prompt row, which
#      ``_retire_unclaimed_resident_activation`` can only retire while it is
#      still open+PENDING+unclaimed.
#
# The age is measured from when the row entered its CURRENT unclaimed state —
# max(created_at, updated_at, handoff timestamp) — not from created_at, because
# session handoff deliberately re-creates this exact row shape from
# arbitrarily old operator messages so the next resident can claim them.

# Default window before an unclaimed PENDING conversation message whose runner
# was actually armed (wakeup_state 'queued'/'coalesced') is considered
# stranded and swept to 'failed'.
STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES = 15

# Default window for rows whose designed answer path is deliberately deferred:
# Chat-mode ('paused'/'waiting_for_capacity') sends answered by the cadence
# strategist, and session-handoff-released rows awaiting the next resident.
# Must exceed the cadence backstop ``strategic_max_interval_minutes`` (default
# 1440) — the effective value below takes the max of the two plus a grace hour.
STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES = 1_500

# Context markers that take a row out of this sweep (or into the deferred
# window). Resolved from the canonical definitions so a rename upstream breaks
# loudly here instead of silently re-widening the predicate.
_STRANDED_SWEEP_CONSULTATION_CLAIM_KEY = "_workday_consultation_claim_id"

# wakeup_state values that mean "a runner really was armed for this send".
# Anything else (including an unstamped legacy row) is treated as deferred.
_STRANDED_SWEEP_ARMED_WAKEUP_STATES = frozenset({"queued", "coalesced"})


def _stranded_sweep_activation_prompt_key() -> str:
    from app.strategic_resident_session import (
        RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY,
    )

    return RESIDENT_ACTIVATION_PROMPT_CONTEXT_KEY


def _stranded_sweep_handoff_at_key() -> str:
    from app.session_message_handoff import RESIDENT_HANDOFF_AT_CONTEXT_KEY

    return RESIDENT_HANDOFF_AT_CONTEXT_KEY


def _stranded_sweep_wakeup_state_key() -> str:
    from app.project_message_projection import WAKEUP_STATE_CONTEXT_KEY

    return WAKEUP_STATE_CONTEXT_KEY


def _stranded_sweep_is_excluded(context: dict) -> bool:
    """True when a row's designed answer path forbids terminal failure here.

    Workday consultation packets/results carry their claim in context rather
    than ``conversation_claim_id`` (so 'unclaimed' is a lie for them), and the
    synthetic activation prompt must stay open+PENDING for
    ``_retire_unclaimed_resident_activation`` to consume it.
    """
    if context.get(_STRANDED_SWEEP_CONSULTATION_CLAIM_KEY):
        return True
    role = context.get("message_role")
    if isinstance(role, str) and role.startswith("workday_consultation"):
        return True
    return context.get(_stranded_sweep_activation_prompt_key()) is True


def _stranded_sweep_unclaimed_since(msg, context: dict) -> datetime:
    """When the row entered its CURRENT unclaimed state.

    Session handoff re-releases arbitrarily old operator messages back to
    PENDING with the claim cleared; ageing on ``created_at`` alone would fail
    them before the next session can rebind them. Take the latest of
    created_at, updated_at, and the recorded handoff timestamp.
    """
    candidates = [msg.created_at]
    updated_at = getattr(msg, "updated_at", None)
    if updated_at is not None:
        candidates.append(updated_at)
    handoff_at = _parse_iso_naive_utc(
        context.get(_stranded_sweep_handoff_at_key())
    )
    if handoff_at is not None:
        candidates.append(handoff_at)
    return max(_stranded_naive_utc(value) for value in candidates)


def _parse_iso_naive_utc(value) -> datetime | None:
    """Parse a stored ISO-8601 context timestamp; None when unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _stranded_naive_utc(parsed)


def _stranded_naive_utc(value: datetime) -> datetime:
    from app.time_utils import _coerce_naive_utc as _coerce

    return _coerce(value)


async def sweep_fail_stranded_pending_conversation_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
) -> int:
    """Step 25: fail stranded operator conversation sends past a bounded age.

    Selects up to ``batch_size`` conversation-surface messages with
    ``from_type == 'user'``, ``handling_state == PENDING``, ``status == 'open'``
    and ``conversation_claim_id IS NULL`` that have been unclaimed longer than
    the applicable window, and transitions each to ``handling_state=FAILED`` /
    ``status='resolved'`` with resolution
    ``'stranded (no runner claim within the window)'``.

    Two windows apply, because two different answer paths exist:

    * armed runner (wakeup_state 'queued'/'coalesced'):
      ``stranded_conversation_message_after_minutes`` (default 15);
    * deferred — Chat-mode 'paused'/'waiting_for_capacity' rows answered by the
      cadence strategist, and session-handoff-released rows awaiting the next
      resident: ``stranded_deferred_conversation_message_after_minutes``
      (default 1500), floored at ``strategic_max_interval_minutes`` + 60 so the
      sweep can never pre-empt the cadence backstop.

    Age is measured from when the row entered its current unclaimed state
    (max of created_at / updated_at / ``_resident_handoff_at``), not creation,
    so a handoff-released message is not destroyed before rebinding.

    System-authored rows (Workday consultation packets and results, whose claim
    lives in context) and the synthetic resident-activation prompt are excluded
    outright. Disjoint from conversation_coordinator's claim-lease-expiry path:
    any message that has been claimed (even with an expired lease) is left
    alone so a replacement generation can still reclaim it. Fail-safe per
    message, idempotent (the transition moves handling_state off PENDING), and
    broadcasts a ``message_updated`` envelope per swept row.

    Returns the number of messages swept to 'failed'.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import MessageHandlingState, MessageSurface, ProjectMessage
    from app.project_message_projection import message_updated_event_payload
    from app.shared_state import runtime_config

    stranded_after_minutes = int(
        runtime_config.get(
            "stranded_conversation_message_after_minutes",
            STRANDED_CONVERSATION_MESSAGE_AFTER_MINUTES,
        )
    )
    cadence_backstop_minutes = int(
        runtime_config.get("strategic_max_interval_minutes", 1_440)
    )
    deferred_after_minutes = max(
        int(
            runtime_config.get(
                "stranded_deferred_conversation_message_after_minutes",
                STRANDED_DEFERRED_CONVERSATION_MESSAGE_AFTER_MINUTES,
            )
        ),
        cadence_backstop_minutes + 60,
        stranded_after_minutes,
    )
    cutoff = now - timedelta(minutes=stranded_after_minutes)
    deferred_cutoff = now - timedelta(minutes=deferred_after_minutes)

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.from_type == "user",
                    ProjectMessage.status == "open",
                    ProjectMessage.handling_state == MessageHandlingState.PENDING,
                    ProjectMessage.conversation_claim_id.is_(None),
                    # Cheap superset of the per-row window test below: the
                    # effective "unclaimed since" is >= both of these.
                    ProjectMessage.created_at <= cutoff,
                    ProjectMessage.updated_at <= cutoff,
                )
                .order_by(ProjectMessage.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) >= batch_size:
        logger.warning(
            "[REAPER] stranded-conversation-message fail sweep hit cap=%d "
            "pending unclaimed messages — result truncated; next cycle continues",
            batch_size,
        )
    if not msgs:
        return 0

    failed = 0
    pending_broadcasts: list[tuple[str, dict]] = []
    for msg in msgs:
        ctx = dict(msg.context or {})
        if _stranded_sweep_is_excluded(ctx):
            continue
        wakeup_state = ctx.get(_stranded_sweep_wakeup_state_key())
        deferred = wakeup_state not in _STRANDED_SWEEP_ARMED_WAKEUP_STATES or bool(
            ctx.get(_stranded_sweep_handoff_at_key())
        )
        applicable_cutoff = deferred_cutoff if deferred else cutoff
        if _stranded_sweep_unclaimed_since(msg, ctx) > applicable_cutoff:
            continue
        msg.handling_state = MessageHandlingState.FAILED
        msg.status = "resolved"
        msg.resolution = "stranded (no runner claim within the window)"
        msg.resolved_at = now
        ctx["_stranded_failed_at"] = now.isoformat()
        msg.context = ctx
        session.add(msg)
        failed += 1
        pending_broadcasts.append(
            (str(msg.project_id), message_updated_event_payload(msg))
        )
        logger.info(
            "[REAPER] Step 25: failed stranded conversation message %s "
            "(unclaimed for >=%dm, deferred=%s)",
            msg.id,
            deferred_after_minutes if deferred else stranded_after_minutes,
            deferred,
        )

    if failed:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] Step 25: broadcast failed for message %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] Step 25: failed %d stranded pending conversation message(s)",
            failed,
        )
    return failed


async def sweep_wake_delivery_stall_alerts(
    session,
    *,
    delivery_stalled: bool,
    stall_cycles: int,
) -> int:
    """Raise or clear per-project wake-delivery-stall operator alerts.

    When ``delivery_stalled`` is True, creates a deduplicated WARNING alert
    for every project that currently has a FIRED (undelivered) registration.
    When False (delivery recovering), dismisses any open stall alerts
    fleet-wide. Returns the number of NEW alerts created.

    Follows the same dedup-on-stable-title pattern as
    :func:`sweep_migration_slot_contention`. Caller owns no commit for the
    create path (commits inside after the loop); the resolve path commits
    per-project inside :func:`alert_dispatch._resolve_wake_delivery_stall_alert`.
    """
    from app.alert_dispatch import _WAKE_DELIVERY_STALL_ALERT_TITLE
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import Alert, AlertSeverity, TaskWakeRegistration, WakeDeliveryState
    from app.time_utils import _utcnow

    if not delivery_stalled:
        # Delivery has recovered — dismiss open stall alerts fleet-wide.
        # Batch-load all open stall alerts in a single query instead of N per-project SELECTs.
        open_alerts = (
            await session.execute(
                select(Alert).where(
                    Alert.dismissed_at.is_(None),
                    Alert.title == _WAKE_DELIVERY_STALL_ALERT_TITLE,
                )
            )
        ).scalars().all()
        dismissed = 0
        if open_alerts:
            now = _utcnow()
            for alert in open_alerts:
                alert.dismissed_at = now
                session.add(alert)
            await session.commit()
            for alert in open_alerts:
                await _bcast(
                    str(alert.project_id),
                    "alert_dismissed",
                    {"alert_id": str(alert.id), "title": alert.title},
                )
            dismissed = len(open_alerts)
        if dismissed:
            logger.info(
                "[REAPER] resolved wake-delivery-stall alert on %d project(s)",
                dismissed,
            )
        return 0

    # Delivery is stalled — raise a deduped alert per project with FIRED backlog.
    result = await session.execute(
        select(TaskWakeRegistration.project_id)
        .distinct()
        .where(TaskWakeRegistration.delivery_state == WakeDeliveryState.FIRED)
    )
    project_ids = result.scalars().all()
    if not project_ids:
        return 0

    # Batch-check which project_ids already have an open stall alert.
    existing_pids = set(
        (
            await session.execute(
                select(Alert.project_id).where(
                    Alert.project_id.in_(project_ids),
                    Alert.dismissed_at.is_(None),
                    Alert.title == _WAKE_DELIVERY_STALL_ALERT_TITLE,
                )
            )
        ).scalars().all()
    )
    title = _WAKE_DELIVERY_STALL_ALERT_TITLE
    msg = (
        f"The wake-delivery sweep has made no progress against the FIRED "
        f"registration backlog for {stall_cycles} consecutive reaper cycles. "
        f"Parked tasks whose wake sources have fired are not being advanced — "
        f"check for a wedged or crashed delivery path."
    )[:1000]
    new_alerts = []
    for pid in project_ids:
        if pid in existing_pids:
            continue
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.WARNING,
            title=title,
            message=msg,
            event_type="deployment.wake_delivery_stalled",
        )
        session.add(alert)
        new_alerts.append(alert)
    created = len(new_alerts)
    if created:
        await session.commit()
        for alert in new_alerts:
            await _bcast(
                str(alert.project_id),
                "alert_created",
                {
                    "alert_id": str(alert.id),
                    "severity": AlertSeverity.WARNING.value,
                    "title": alert.title,
                },
            )
        logger.info(
            "[REAPER] raised wake-delivery-stall alert on %d project(s)",
            created,
        )
    return created
