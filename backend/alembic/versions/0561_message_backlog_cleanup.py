"""One-time message-backlog auto-resolve cleanup (design d-7c851b3f, Stage 2 step 4).

WHY THIS MIGRATION EXISTS
-------------------------
The unified operator-audience routing + auto-clearing message lifecycle design
(knowledge doc f8b60a6e-4c45-4d14-9126-ffb5432246a4) makes ``needs_action`` a
DERIVED property and routes every emitter through
``app.message_audience.route_event``. The sweeps that keep the operator
open/needs-action count honest going forward — the generalized linked-resolve
sweep and the stale-open decay sweep in ``reaper_sweeps.py`` — only act on NEW
activity; they never retro-clear the ~50 open v22-era ``ProjectMessage`` rows
that predate the routing registry. This ONE-TIME data migration clears that
historical backlog so the operator's open/needs-action count reflects only
genuinely-live threads (the design's success evidence).

WHAT IT DOES (exact order — see the task spec)
----------------------------------------------
1. Bulk auto-resolve OPEN system-authored NOTE rows whose ``event_type`` (stored
   in ``context['event_type']``) reclassifies to ``strategist-internal`` or
   ``pipeline-log`` under ``route_event`` — these should never have been
   operator-open. Stamped ``resolution='auto-resolved (reclassified strategist-internal)'``.
   route_event fails closed to strategist-internal, so an unregistered legacy
   event_type is treated as non-operator and cleared; operator-visible
   (operator-fyi / operator-action-needed) NOTEs are LEFT untouched.
2. Auto-resolve conversational NOTE/FEEDBACK rows older than the stale-open decay
   window (the SAME ``STALE_OPEN_DECAY_DAYS`` constant the decay sweep uses,
   default 14 days) that carry no live ``_convergence_link`` condition. Stamped
   ``resolution='auto-decayed (stale)'`` — byte-identical to the sweep.
3. LEAVE genuinely-live QUESTION/BLOCKER rows untouched — their ``needs_action``
   is re-derived at read time, never stored, so nothing here touches them.

This is a DATA migration: no schema change (``needs_action`` is derived, not a
column). It is idempotent — every UPDATE guards on ``status = 'open'``, so an
already-resolved row is skipped and re-running is a no-op. ``downgrade`` is a
deliberate no-op: a one-time backlog cleanup is not reversed (re-opening
auto-resolved threads would resurrect exactly the noise this removes).

Imports ``app.message_audience`` (standalone, no candidate-code deps) and the
lifecycle constants from ``app.message_convergence`` / ``app.reaper_sweeps`` so
the reclassification and decay decisions match the live sweeps exactly.

Revision ID: 0561
Revises: 0533
Create Date: 2026-07-14 00:00:00.000000

"""

import json
import logging
from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

# NOTE: 0561 is the revision id RESERVED for this task (authored in parallel;
# linearized against production at merge time). It chains from the current head
# 0547 to keep the chain linear.
revision = "0561"
down_revision = "0547"
branch_labels = None
depends_on = None


# Resolution audit strings. Step-2's value is byte-identical to the stale-open
# decay sweep (``reaper_sweeps.sweep_decay_stale_open_messages``) so the backlog
# cleanup and the ongoing sweep stamp the same reason.
_RECLASSIFY_RESOLUTION = "auto-resolved (reclassified strategist-internal)"
_DECAY_RESOLUTION = "auto-decayed (stale)"


def _load_context(raw) -> dict:
    """Return a dict for a ``context`` cell across dialects.

    Postgres (JSONB) hands back a ``dict``; sqlite (TEXT) hands back a JSON
    string; either can be ``None``. Malformed JSON is treated as empty so a
    single bad row never aborts the whole one-time cleanup.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_reclassified_system_notes(bind, now: datetime) -> int:
    """Step 1 — clear open system NOTEs that route to a non-operator audience."""
    from app.message_audience import PIPELINE_LOG, STRATEGIST_INTERNAL, route_event

    non_operator = {STRATEGIST_INTERNAL, PIPELINE_LOG}
    rows = bind.execute(
        sa.text(
            "SELECT id, context FROM projectmessage "
            "WHERE status = 'open' AND from_type = 'system' "
            "AND message_type = 'NOTE'"
        )
    ).fetchall()

    resolved = 0
    for row_id, raw_ctx in rows:
        ctx = _load_context(raw_ctx)
        event_type = str(ctx.get("event_type") or "").strip()
        if not event_type:
            # No event_type to classify — leave it (may still age out in step 2).
            continue
        if route_event(event_type) not in non_operator:
            # Operator-visible NOTE — genuinely-live, do not touch.
            continue
        result = bind.execute(
            sa.text(
                "UPDATE projectmessage "
                "SET status = 'resolved', resolution = :res, resolved_at = :now "
                "WHERE id = :id AND status = 'open'"
            ),
            {"res": _RECLASSIFY_RESOLUTION, "now": now, "id": row_id},
        )
        resolved += result.rowcount or 0
    return resolved


def _decay_stale_conversational(bind, now: datetime) -> int:
    """Step 2 — age out stale conversational NOTE/FEEDBACK with no live link."""
    from app.message_convergence import CONVERSATIONAL_DECAY_MESSAGE_TYPES

    try:
        from app.reaper_sweeps import STALE_OPEN_DECAY_DAYS as decay_days
    except Exception:  # pragma: no cover - defensive; constant is stable
        decay_days = 14

    decay_types = set(CONVERSATIONAL_DECAY_MESSAGE_TYPES)
    cutoff = now - timedelta(days=int(decay_days))

    rows = bind.execute(
        sa.text(
            "SELECT id, message_type, context FROM projectmessage "
            "WHERE status = 'open' AND created_at <= :cutoff"
        ),
        {"cutoff": cutoff},
    ).fetchall()

    resolved = 0
    for row_id, message_type, raw_ctx in rows:
        # Only conversational NOTE/FEEDBACK age out. QUESTION/BLOCKER carry a live
        # operator obligation and are never decayed; BUG_REPORT is owned by the
        # bug-class convergence sweeps.
        if str(message_type) not in decay_types:
            continue
        # Never decay a row that still carries a live convergence link.
        if isinstance(_load_context(raw_ctx).get("_convergence_link"), dict):
            continue
        result = bind.execute(
            sa.text(
                "UPDATE projectmessage "
                "SET status = 'resolved', resolution = :res, resolved_at = :now "
                "WHERE id = :id AND status = 'open'"
            ),
            {"res": _DECAY_RESOLUTION, "now": now, "id": row_id},
        )
        resolved += result.rowcount or 0
    return resolved


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    reclassified = _resolve_reclassified_system_notes(bind, now)
    decayed = _decay_stale_conversational(bind, now)

    logger.info(
        "Migration 0561 backlog cleanup: reclassified=%d stale-decayed=%d "
        "(live QUESTION/BLOCKER threads left untouched)",
        reclassified,
        decayed,
    )


def downgrade() -> None:
    # No-op by design: a ONE-TIME backlog cleanup is not reversed. Re-opening
    # auto-resolved rows would resurrect exactly the operator-facing noise this
    # migration removes, and the resolutions are recorded in the audit trail.
    logger.info(
        "Migration 0561 downgrade is a no-op — one-time backlog cleanup is not "
        "reversed."
    )
