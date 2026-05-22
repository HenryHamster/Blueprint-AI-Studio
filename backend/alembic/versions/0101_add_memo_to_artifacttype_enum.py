"""Create artifacttype enum and add MEMO value.

The ArtifactType enum in Python (backend/app/models.py) now includes
MEMO as a valid artifact type. This migration:

1. Creates the artifacttype Postgres enum type with all valid values
   (including the new MEMO value)

No CHECK constraint or column alteration is applied — the enum type is
created so the value is available; the artifact table is unchanged.

Revision ID: 0101
Revises: 0100
Create Date: 2026-05-21 00:00:00.000000

"""

from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


# All ArtifactType enum values as of this migration (mirrors
# ArtifactType in backend/app/models.py)
_ARTIFACT_TYPE_VALUES = (
    "GIT_COMMIT",
    "PULL_REQUEST",
    "SCREENSHOT",
    "ERROR_LOG",
    "RESEARCH_FINDINGS",
    "SECURITY_REPORT",
    "SMOKETEST_RESULT",
    "DEBUG_RESULT",
    "PERF_REPORT",
    "INVESTIGATION_REPORT",
    "SELF_AUDIT_REPORT",
    "RELEASE_AUDIT_REPORT",
    "MEMO",
)


def upgrade() -> None:
    # Create the artifacttype enum type with all valid values.
    # Use autocommit to bypass transaction requirement.
    with op.get_context().autocommit_block():
        # Construct the enum creation statement
        values_str = ", ".join(f"'{v}'" for v in _ARTIFACT_TYPE_VALUES)
        op.execute(f"CREATE TYPE artifacttype AS ENUM ({values_str})")


def downgrade() -> None:
    # Drop the enum type. Postgres cascades the drop to columns that use it.
    with op.get_context().autocommit_block():
        op.execute("DROP TYPE IF EXISTS artifacttype CASCADE")
