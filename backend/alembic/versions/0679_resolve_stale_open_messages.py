"""Resolve stale open conversational messages from the v22-era backlog.

A one-time data backfill that applies the same lifecycle rules as
app.reaper_sweeps.sweep_decay_stale_open_messages to clear the ~50-message
stale open ProjectMessage backlog (mostly dead v22-era conversational threads)
from the operator inbox.

Lifecycle rules (mirrors sweep_decay_stale_open_messages exactly):
- NOTE/FEEDBACK >= 14 days old, not operator-action-needed
  → resolution 'auto-decayed (stale)'
- NOTE/FEEDBACK with audience='operator-action-needed' >= 60 days old
  → resolution 'auto-decayed (stale)'
- QUESTION >= 30 days old → resolution 'stale_unanswered'
- Messages with a live _convergence_link in context are preserved.
- BLOCKER and BUG_REPORT message types are never touched (not in
  CONVERSATIONAL_DECAY_MESSAGE_TYPES).

Idempotent: only targets status='open', so re-running finds nothing to update.

Data is never deleted — only status/resolution/resolved_at/context are updated
on rows that were genuinely stale. Reversible in spirit; downgrade is a no-op
because original resolution/resolved_at values cannot be reconstructed.

Revision ID: 0679
Revises: 0672
Create Date: 2026-07-22

See also:
- app.reaper_sweeps.sweep_decay_stale_open_messages
- app.message_convergence.CONVERSATIONAL_DECAY_MESSAGE_TYPES
- app.message_convergence.get_message_link
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alembic import op
import sqlalchemy as sa

revision = "0679"
down_revision = "0672"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Use a single fixed-timestamp so all resolved rows share the same time.
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC
    now_iso = now.isoformat()
    note_cutoff = now - timedelta(days=14)
    operator_cutoff = now - timedelta(days=60)
    question_cutoff = now - timedelta(days=30)

    # 1. Stale NOTE/FEEDBACK not requiring operator action (14-day decay)
    conn.execute(
        sa.text(
            """UPDATE projectmessage
               SET status = 'resolved',
                   resolution = 'auto-decayed (stale)',
                   resolved_at = :now,
                   context = COALESCE(context, '{}'::jsonb)
                             || jsonb_build_object('_convergence_converged_at', CAST(:now_iso AS text))
               WHERE status = 'open'
                 AND message_type IN ('NOTE', 'FEEDBACK')
                 AND created_at <= :cutoff
                 AND NOT COALESCE(context, '{}'::jsonb) ? '_convergence_link'
                 AND (COALESCE(context, '{}'::jsonb)->>'audience')
                     IS DISTINCT FROM 'operator-action-needed'"""
        ),
        {"now": now, "now_iso": now_iso, "cutoff": note_cutoff},
    )

    # 2. Stale operator-action-needed NOTE/FEEDBACK (60-day decay)
    conn.execute(
        sa.text(
            """UPDATE projectmessage
               SET status = 'resolved',
                   resolution = 'auto-decayed (stale)',
                   resolved_at = :now,
                   context = COALESCE(context, '{}'::jsonb)
                             || jsonb_build_object('_convergence_converged_at', CAST(:now_iso AS text))
               WHERE status = 'open'
                 AND message_type IN ('NOTE', 'FEEDBACK')
                 AND created_at <= :cutoff
                 AND NOT COALESCE(context, '{}'::jsonb) ? '_convergence_link'
                 AND (COALESCE(context, '{}'::jsonb)->>'audience')
                     = 'operator-action-needed'"""
        ),
        {"now": now, "now_iso": now_iso, "cutoff": operator_cutoff},
    )

    # 3. Stale unanswered QUESTION (30-day decay)
    conn.execute(
        sa.text(
            """UPDATE projectmessage
               SET status = 'resolved',
                   resolution = 'stale_unanswered',
                   resolved_at = :now,
                   context = COALESCE(context, '{}'::jsonb)
                             || jsonb_build_object('_convergence_converged_at', CAST(:now_iso AS text))
               WHERE status = 'open'
                 AND message_type = 'QUESTION'
                 AND created_at <= :cutoff
                 AND NOT COALESCE(context, '{}'::jsonb) ? '_convergence_link'"""
        ),
        {"now": now, "now_iso": now_iso, "cutoff": question_cutoff},
    )


def downgrade() -> None:
    # Conceptual reversal is not cleanly expressible in a single pass: we
    # cannot reconstruct the original resolution/resolved_at values, and the
    # automated sweep (sweep_decay_stale_open_messages) re-applies the same
    # lifecycle rules on the next reaper cycle.  Data is never deleted — only
    # status/resolution/resolved_at/context are updated on rows that were
    # genuinely stale at migration time.
    pass
