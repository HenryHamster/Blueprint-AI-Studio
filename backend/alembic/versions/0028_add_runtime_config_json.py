"""add_runtime_config_json

Persist dict/list runtime config fields (task_type_models, task_type_efforts,
agent_prompt_suffixes, etc.) to the database so they survive Cloud Run restarts.

Revision ID: 0028
Revises: 0027
Create Date: 2026-04-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('deploymentstate', sa.Column('runtime_config_json', JSONB, nullable=True))

    # Backfill with empty defaults — these fields were never persisted before
    op.execute("""
        UPDATE deploymentstate
        SET runtime_config_json = '{
            "task_type_models": {"research": "claude-opus-4-6"},
            "task_type_efforts": {"research": "max"},
            "agent_prompt_suffixes": {},
            "autonomous_periodic_tasks": {"security_audit": {"interval_hours": 168, "enabled": false}, "qa": {"interval_hours": 72, "enabled": false}, "deps": {"interval_hours": 168, "enabled": false}},
            "health_check_urls": [],
            "autonomous_research_wave_size": 3
        }'::jsonb
        WHERE id = 1 AND runtime_config_json IS NULL
    """)


def downgrade() -> None:
    op.drop_column('deploymentstate', 'runtime_config_json')
