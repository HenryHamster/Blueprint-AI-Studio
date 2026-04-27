"""Add strategic cycle table; backfill from project.vision._strategic_cycle_history

Audit 2026-04-25 W2: cycles previously lived in a 10-entry sliding JSONB list
on ``Project.vision["_strategic_cycle_history"]``. The cap silently dropped
old entries — including ones stuck in ``outcome="in_progress"`` from phase1
crashes that never reached the completion handler — so the strategic agent
had no diagnostic signal that cycles were failing.

Promotes the cycle history to a real ``strategiccycle`` table:
- Cycles can be queried by phase, outcome, or age (indexes for the
  stalled-cycle reaper that the same audit lap adds).
- ``Task.strategic_cycle_id`` (added in 0045) becomes the FK linkage so
  task-lifecycle terminal-status hooks can backstop the synchronous
  phase1→phase2 chain when it crashes mid-flight.
- Backfill copies whatever survived in ``vision["_strategic_cycle_history"]``;
  per the user's call (2026-04-26), lost history beyond the 10-entry cap
  is acceptable casualty.

Cycle entry fields previously stored in JSONB are split:
- Hot/queryable: dedicated columns (outcome, phase1/2_task_id+started_at+
  completed_at, directions_created/retired, tasks_spawned, vision_updated,
  cycle_reasoning, summary_text, failure_reason).
- Free-form/per-direction maps (created_direction_ids list, retired
  rationales dict, predictions dict, baseline_vision snapshot for 3-way
  merge): kept as ``cycle_metadata`` JSONB.

Revision ID: 0047
Revises: 0046
Create Date: 2026-04-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '0047'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'strategiccycle',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'project_id', sa.Uuid(),
            sa.ForeignKey('project.id', ondelete='CASCADE'),
            nullable=False,
        ),
        # Phase tracking. *_started_at records when each phase task was
        # spawned (task creation time); *_completed_at records when the task
        # reached a terminal status. NULL means that phase hasn't started/finished.
        sa.Column('outcome', sa.String(length=32), nullable=False, server_default='in_progress'),
        sa.Column('phase1_task_id', sa.Uuid(),
                  sa.ForeignKey('task.id', ondelete='SET NULL'), nullable=True),
        sa.Column('phase1_started_at', sa.DateTime(timezone=False), nullable=False),
        sa.Column('phase1_completed_at', sa.DateTime(timezone=False), nullable=True),
        sa.Column('phase2_task_id', sa.Uuid(),
                  sa.ForeignKey('task.id', ondelete='SET NULL'), nullable=True),
        sa.Column('phase2_started_at', sa.DateTime(timezone=False), nullable=True),
        sa.Column('phase2_completed_at', sa.DateTime(timezone=False), nullable=True),
        # Outcome accounting (filled in on phase 2 completion).
        sa.Column('directions_created', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('directions_retired', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tasks_spawned', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('vision_updated', sa.Boolean(), nullable=False, server_default=sa.false()),
        # Free-form text fields surfaced to the UI / next cycle's context.
        sa.Column('cycle_reasoning', sa.Text(), nullable=True),
        sa.Column('summary_text', sa.Text(), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        # Per-direction maps + baseline_vision snapshot for 3-way merge.
        sa.Column('cycle_metadata', JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=False), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=False), nullable=False, server_default=sa.func.now()),
    )

    # Most common query: "last N cycles for this project, newest first".
    op.create_index(
        'ix_strategiccycle_project_started',
        'strategiccycle',
        ['project_id', sa.text('phase1_started_at DESC')],
    )
    # Stalled-cycle reaper scans active outcomes only.
    op.create_index(
        'ix_strategiccycle_outcome_inprogress',
        'strategiccycle',
        ['outcome', 'phase1_started_at'],
        postgresql_where=sa.text("outcome IN ('in_progress', 'phase1_running', 'phase2_running')"),
    )
    # Lifecycle-hook lookup: given a task that just terminated, find its cycle.
    op.create_index(
        'ix_strategiccycle_phase1_task',
        'strategiccycle', ['phase1_task_id'],
        postgresql_where=sa.text('phase1_task_id IS NOT NULL'),
    )
    op.create_index(
        'ix_strategiccycle_phase2_task',
        'strategiccycle', ['phase2_task_id'],
        postgresql_where=sa.text('phase2_task_id IS NOT NULL'),
    )

    # ---------------------------------------------------------------
    # Backfill from project.vision._strategic_cycle_history
    # ---------------------------------------------------------------
    # Each entry has fields described in strategic_spawning.py:660-672 and
    # progressively filled by strategic_completion.py. Best-effort parse;
    # rows with missing required fields are skipped (we accept lost history).
    op.execute(r"""
        INSERT INTO strategiccycle (
            id, project_id, outcome,
            phase1_task_id, phase1_started_at, phase1_completed_at,
            phase2_task_id, phase2_started_at, phase2_completed_at,
            directions_created, directions_retired, tasks_spawned,
            vision_updated, cycle_reasoning, summary_text,
            cycle_metadata, created_at, updated_at
        )
        SELECT
            COALESCE(c->>'cycle_id', gen_random_uuid()::text),
            p.id,
            COALESCE(c->>'outcome', 'in_progress'),
            CASE WHEN c->>'phase1_task_id' ~ '^[0-9a-f-]{36}$'
                 THEN (c->>'phase1_task_id')::uuid ELSE NULL END,
            COALESCE(
                CASE WHEN c->>'started_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                     THEN (substring(c->>'started_at' from '^[0-9T:\-\.]+'))::timestamp
                     ELSE NULL END,
                NOW()
            ),
            CASE WHEN c->>'phase1_completed_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                 THEN (substring(c->>'phase1_completed_at' from '^[0-9T:\-\.]+'))::timestamp
                 ELSE NULL END,
            CASE WHEN c->>'phase2_task_id' ~ '^[0-9a-f-]{36}$'
                 THEN (c->>'phase2_task_id')::uuid ELSE NULL END,
            -- phase2_started_at not stored in the JSONB shape; use phase1_completed_at
            -- as a proxy (phase 2 spawned synchronously at phase 1 completion).
            CASE WHEN c->>'phase1_completed_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                 THEN (substring(c->>'phase1_completed_at' from '^[0-9T:\-\.]+'))::timestamp
                 ELSE NULL END,
            CASE WHEN c->>'phase2_completed_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                 THEN (substring(c->>'phase2_completed_at' from '^[0-9T:\-\.]+'))::timestamp
                 ELSE NULL END,
            COALESCE((c->>'directions_created')::int, 0),
            COALESCE((c->>'directions_retired')::int, 0),
            COALESCE((c->>'tasks_spawned')::int, 0),
            COALESCE((c->>'vision_updated')::boolean, FALSE),
            c->>'cycle_reasoning',
            c->>'summary_text',
            -- Capture all the per-direction maps + extra fields as metadata.
            c - 'cycle_id' - 'outcome' - 'phase1_task_id' - 'phase1_completed_at'
              - 'phase2_task_id' - 'phase2_completed_at' - 'started_at'
              - 'directions_created' - 'directions_retired' - 'tasks_spawned'
              - 'vision_updated' - 'cycle_reasoning' - 'summary_text',
            COALESCE(
                CASE WHEN c->>'started_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                     THEN (substring(c->>'started_at' from '^[0-9T:\-\.]+'))::timestamp
                     ELSE NULL END,
                NOW()
            ),
            NOW()
        FROM project p,
             jsonb_array_elements(COALESCE(p.vision->'_strategic_cycle_history', '[]'::jsonb)) AS c
        WHERE p.vision IS NOT NULL
          -- Skip entries missing the required cycle_id+started_at; lost history is acceptable.
          AND COALESCE(c->>'cycle_id', '') <> ''
        ON CONFLICT (id) DO NOTHING;
    """)

    # Strip _strategic_cycle_history from vision; the table is the source of truth.
    op.execute(
        "UPDATE project SET vision = vision - '_strategic_cycle_history' "
        "WHERE vision ? '_strategic_cycle_history';"
    )


def downgrade() -> None:
    op.drop_index('ix_strategiccycle_phase2_task', table_name='strategiccycle')
    op.drop_index('ix_strategiccycle_phase1_task', table_name='strategiccycle')
    op.drop_index('ix_strategiccycle_outcome_inprogress', table_name='strategiccycle')
    op.drop_index('ix_strategiccycle_project_started', table_name='strategiccycle')
    op.drop_table('strategiccycle')
