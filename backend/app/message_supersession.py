"""Auto-supersede stale operator messages when a release version closes.

The strategist open-messages backlog (``./fetch message`` and the admin-ui
messages view) accumulates ``ProjectMessage`` rows across the *entire* release
lifecycle. A strategic status update, a churn-guardrail note, or a back-and-forth
conversation thread authored during v22's build phase stays ``status="open"``
forever after v22 promotes and v23 opens — even though its content is
definitionally superseded (the version it discusses is no longer the open one).
Manually resolving these one at a time does not scale and recurs every cycle.

This module ties message relevance to the release-version lifecycle. When a
version transitions to a terminal state (``promoted`` / ``abandoned``), the
promotion/abandon path calls :func:`supersede_stale_messages_on_version_close`,
which resolves still-open, non-bug-class messages that were created *strictly
before* the closing version's window — i.e. they belong to a version at least
``min_versions_back`` back — and are older than ``min_age_days``.

Safety, mirroring the observe-mode-first precedent already shipped in
``metrics_integrity_sweep`` and ``structural_health_audit``:

* **Mode-gated** — ``message_supersession_mode`` (off / observe / enforce),
  default ``observe``. ``observe`` measures the would-be resolutions and records
  nothing; only ``enforce`` mutates rows.
* **Wide safety margin** — a message is only superseded when it is BOTH older
  than ``min_age_days`` (default 14) AND from a version at least
  ``min_versions_back`` back (default 2). Both conditions must hold, so nothing
  from the current or immediately-prior version is ever touched.
* **Never resolves an unresolved bug** — ``BUG_REPORT`` / ``BLOCKER`` messages
  that are still ``open`` (i.e. NOT yet converged/linked to a fix, per
  ``message_convergence``) are always skipped. A bug does not become fixed just
  because a version promoted; a still-reproducing defect must stay open across
  the version boundary. Bug-class messages that were converged are already
  ``resolved``/``linked`` and therefore not in the ``open`` candidate set at all.

No schema change: the supersession provenance is stamped into the existing
``ProjectMessage.resolution`` / ``resolved_at`` columns and a ``context`` audit
key. No new DB columns or migrations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import select

from app.message_convergence import OPEN_STATUS, is_bug_class
from app.models import ProjectMessage
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Mode vocabulary — mirrors metrics_integrity_sweep / structural_health_audit.
SUPERSESSION_MODES: frozenset[str] = frozenset({"off", "observe", "enforce"})
DEFAULT_MODE = "observe"

# A message must be at least this many days old to be eligible (safety margin).
DEFAULT_MIN_AGE_DAYS = 14
# A message must belong to a version at least this many versions behind the
# current open version. 2 => never touch the current or immediately-prior
# version's messages.
DEFAULT_MIN_VERSIONS_BACK = 2
# Upper bound on rows resolved per version-close (backstop against a runaway
# batch; a truncated run simply resolves the rest at the next version close).
DEFAULT_MAX_RESOLVE = 500

# JSONB context key recording the auto-supersession audit trail.
SUPERSEDED_CONTEXT_KEY = "_superseded_by_version_close"


def _resolve_config() -> dict:
    """Resolve the runtime-config-driven supersession knobs (mode / margins)."""
    from app.shared_state import runtime_config as _runtime_config

    mode = str(_runtime_config.get("message_supersession_mode", DEFAULT_MODE)).lower()
    if mode not in SUPERSESSION_MODES:
        mode = DEFAULT_MODE

    try:
        min_age_days = int(
            _runtime_config.get("message_supersession_min_age_days", DEFAULT_MIN_AGE_DAYS)
        )
    except (TypeError, ValueError):
        min_age_days = DEFAULT_MIN_AGE_DAYS
    if min_age_days < 0:
        min_age_days = DEFAULT_MIN_AGE_DAYS

    try:
        min_versions_back = int(
            _runtime_config.get(
                "message_supersession_min_versions_back", DEFAULT_MIN_VERSIONS_BACK
            )
        )
    except (TypeError, ValueError):
        min_versions_back = DEFAULT_MIN_VERSIONS_BACK
    if min_versions_back < 1:
        min_versions_back = DEFAULT_MIN_VERSIONS_BACK

    try:
        max_resolve = int(
            _runtime_config.get("message_supersession_max_resolve", DEFAULT_MAX_RESOLVE)
        )
    except (TypeError, ValueError):
        max_resolve = DEFAULT_MAX_RESOLVE
    if max_resolve <= 0:
        max_resolve = DEFAULT_MAX_RESOLVE

    return {
        "mode": mode,
        "min_age_days": min_age_days,
        "min_versions_back": min_versions_back,
        "max_resolve": max_resolve,
    }


async def _resolve_version_cutoff(
    session,
    *,
    project_id,
    closed_version_number: int,
    closed_version_opened_at: datetime,
    min_versions_back: int,
    now: datetime,
) -> datetime:
    """Return the ``created_at`` upper bound for the version-distance condition.

    A message "belongs to" version V when it was created during V's open window
    (``opened_at(V) <= created_at < opened_at(V+1)``). The current open version
    is ``closed_version_number + 1`` (the successor opens as part of the same
    transition). To supersede only messages from versions at least
    ``min_versions_back`` behind the current open version, the boundary is
    ``opened_at`` of version ``current_open - min_versions_back + 1``.

    For the default ``min_versions_back == 2`` this is exactly the just-closed
    version's own ``opened_at`` (passed in — no query). For a larger margin the
    boundary is an older version and is looked up; if that row is missing the
    fallback is the just-closed version's ``opened_at``, which is *later* and so
    strictly *more* conservative (fewer messages qualify). For ``== 1`` (less
    safe, includes the closed version's own messages) the boundary is the
    current open version, which opened at ~``now``.
    """
    current_open_number = closed_version_number + 1
    boundary_number = current_open_number - min_versions_back + 1

    if boundary_number > closed_version_number:
        # min_versions_back <= 1: boundary is the just-opened successor (~now).
        return now
    if boundary_number == closed_version_number:
        # Default path (min_versions_back == 2): no extra query needed.
        return closed_version_opened_at

    # boundary_number < closed_version_number: an older version — look it up.
    from app.release_models import ReleaseVersion

    opened_at = (
        (
            await session.execute(
                select(ReleaseVersion.opened_at).where(
                    ReleaseVersion.project_id == project_id,
                    ReleaseVersion.version_number == boundary_number,
                )
            )
        )
        .scalars()
        .first()
    )
    if opened_at is None:
        # Older row pruned/missing — fall back to the stricter (later) boundary.
        return closed_version_opened_at
    return opened_at


async def supersede_stale_messages_on_version_close(
    session,
    *,
    project_id,
    closed_version_number: int,
    closed_version_opened_at: datetime,
    terminal_status: str,
    now: datetime | None = None,
) -> dict:
    """Auto-resolve still-open, non-bug-class messages superseded by a version close.

    Called from the promotion / abandon path once a version reaches a terminal
    state. Mode-gated (off / observe / enforce). In ``enforce`` it stamps each
    eligible message ``status="resolved"`` with a system-generated resolution and
    ``resolved_at``; in ``observe`` it only measures the would-be set and mutates
    nothing. Bug-class (``BUG_REPORT`` / ``BLOCKER``) open messages are always
    skipped — an unresolved bug stays open across the version boundary.

    Does NOT commit — the caller owns the transaction. Returns a summary dict; in
    ``enforce`` the ``broadcasts`` key carries ``(project_id, payload)`` tuples
    the caller should emit as ``message_updated`` events *after* committing.
    """
    cfg = _resolve_config()
    mode = cfg["mode"]
    _now = now or _utcnow()

    summary: dict = {
        "mode": mode,
        "version_number": closed_version_number,
        "terminal_status": terminal_status,
        "acted": False,
        "candidates": 0,
        "superseded": 0,
        "skipped_bug_class": 0,
        "truncated": False,
        "broadcasts": [],
    }
    if mode == "off":
        return summary

    version_cutoff = await _resolve_version_cutoff(
        session,
        project_id=project_id,
        closed_version_number=closed_version_number,
        closed_version_opened_at=closed_version_opened_at,
        min_versions_back=cfg["min_versions_back"],
        now=_now,
    )
    age_cutoff = _now - timedelta(days=cfg["min_age_days"])
    # Both conditions must hold (older than N days AND at least K versions back),
    # so the effective cutoff is the earlier of the two thresholds.
    effective_cutoff = min(age_cutoff, version_cutoff)

    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.created_at < effective_cutoff,
                )
                .order_by(ProjectMessage.created_at)
                .limit(cfg["max_resolve"])
            )
        )
        .scalars()
        .all()
    )
    if len(rows) >= cfg["max_resolve"]:
        summary["truncated"] = True
        logger.warning(
            "[MSG_SUPERSEDE] hit cap=%d open messages before v%d %s (project=%s) — "
            "result truncated; next version close continues",
            cfg["max_resolve"],
            closed_version_number,
            terminal_status,
            project_id,
        )

    eligible: list[ProjectMessage] = []
    skipped_bug = 0
    for msg in rows:
        # Never auto-resolve an unresolved bug: a still-open BUG_REPORT/BLOCKER
        # has not been converged/linked to a fix (a converged one would already
        # be resolved/linked, not open). A bug does not become fixed because a
        # version promoted.
        if is_bug_class(msg):
            skipped_bug += 1
            continue
        eligible.append(msg)

    summary["candidates"] = len(eligible)
    summary["skipped_bug_class"] = skipped_bug

    resolution = (
        f"Auto-superseded: created before v{closed_version_number} "
        f"{terminal_status}; the version it referenced is no longer the open "
        f"version."
    )

    if mode == "observe":
        # Measure only — record the would-be count, mutate nothing.
        summary["superseded"] = len(eligible)
        logger.info(
            "[MSG_SUPERSEDE] observe: %d open message(s) would be superseded by "
            "v%d %s (project=%s); %d bug-class skipped",
            len(eligible),
            closed_version_number,
            terminal_status,
            project_id,
            skipped_bug,
        )
        return summary

    # enforce: stamp each eligible message resolved.
    for msg in eligible:
        msg.status = "resolved"
        msg.resolution = resolution
        msg.resolved_at = _now
        ctx = dict(msg.context or {})
        ctx[SUPERSEDED_CONTEXT_KEY] = {
            "version_number": closed_version_number,
            "terminal_status": terminal_status,
            "resolved_at": _now.isoformat(),
        }
        msg.context = ctx
        session.add(msg)

    summary["acted"] = bool(eligible)
    summary["superseded"] = len(eligible)
    # Emit ONE aggregate broadcast instead of N individual message_updated events.
    # Each individual broadcast would fire a live notification for every resolved
    # message — with up to message_supersession_max_resolve (default 500) messages
    # per version close, that's a broadcast storm. The frontend only needs to know
    # that some messages changed so it can re-fetch; a single summary event suffices.
    if eligible:
        summary["broadcasts"] = [
            (
                str(project_id),
                {
                    "count": len(eligible),
                    "version_number": closed_version_number,
                    "terminal_status": terminal_status,
                    "message_ids": [str(msg.id) for msg in eligible],
                },
            )
        ]
    if eligible:
        logger.info(
            "[MSG_SUPERSEDE] enforce: auto-superseded %d open message(s) on v%d "
            "%s (project=%s); %d bug-class left open",
            len(eligible),
            closed_version_number,
            terminal_status,
            project_id,
            skipped_bug,
        )
    return summary


async def run_supersession_on_version_close(
    *,
    project_id,
    closed_version_number: int,
    closed_version_opened_at: datetime,
    terminal_status: str,
    now: datetime | None = None,
) -> dict | None:
    """Best-effort wrapper: run supersession in its OWN session, commit, broadcast.

    Designed to be called from the promotion / abandon path, where the closing
    version's own writes ride the caller's later commit and are not yet durable —
    so this must run in a dedicated session (mirroring the staged-direction
    activation and lane-baseline snapshot best-effort blocks). Every failure is
    swallowed and logged: message supersession must NEVER abort or fail a
    promotion / abandon. Returns the summary dict, or ``None`` on error / when the
    lane is ``off``.
    """
    from app.broadcast import broadcast_to_project
    from app.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as _session:
            summary = await supersede_stale_messages_on_version_close(
                _session,
                project_id=project_id,
                closed_version_number=closed_version_number,
                closed_version_opened_at=closed_version_opened_at,
                terminal_status=terminal_status,
                now=now,
            )
            broadcasts = summary.get("broadcasts") or []
            if summary.get("acted"):
                await _session.commit()
            # broadcasts contains at most ONE aggregate entry (pid, summary_payload);
            # emit it as messages_superseded rather than N individual message_updated
            # events to avoid a broadcast storm on large stale backlogs.
            for pid, payload in broadcasts:
                try:
                    await broadcast_to_project(pid, "messages_superseded", payload)
                except Exception as _bcast_err:  # noqa: BLE001, PERF203
                    logger.warning(
                        "[MSG_SUPERSEDE] aggregate broadcast failed for %d superseded "
                        "message(s) on v%d: %s",
                        payload.get("count", 0),
                        closed_version_number,
                        _bcast_err,
                    )
            return summary
    except Exception as exc:  # noqa: BLE001 — supersession must not abort the transition
        logger.warning(
            "[MSG_SUPERSEDE] supersession failed after v%d %s (non-fatal; next "
            "version close retries): %s",
            closed_version_number,
            terminal_status,
            exc,
        )
        return None
