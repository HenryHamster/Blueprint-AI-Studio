"""add_llm_provider_fields

Extend maxplanaccount with provider-aware fields for multi-provider LLM account
groundwork. Existing Anthropic rows are backfilled to provider=anthropic,
auth_mode=oauth, and secret_blob_ref=oauth_token.

Revision ID: 0039
Revises: 0038
Create Date: 2026-04-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


provider_enum = sa.Enum("anthropic", "openai", "gemini", name="llmprovider")
auth_mode_enum = sa.Enum("oauth", "api_key", "service_account", name="llmauthmode")


def upgrade() -> None:
    bind = op.get_bind()
    provider_enum.create(bind, checkfirst=True)
    auth_mode_enum.create(bind, checkfirst=True)

    op.add_column(
        "maxplanaccount",
        sa.Column("provider", provider_enum, nullable=False, server_default="anthropic"),
    )
    op.add_column(
        "maxplanaccount",
        sa.Column("auth_mode", auth_mode_enum, nullable=False, server_default="oauth"),
    )
    op.add_column("maxplanaccount", sa.Column("secret_blob_ref", sa.String(), nullable=True))
    op.add_column("maxplanaccount", sa.Column("provider_metadata", JSONB, nullable=True))
    op.alter_column("maxplanaccount", "oauth_token", existing_type=sa.String(), nullable=True)

    op.execute(
        """
        UPDATE maxplanaccount
        SET secret_blob_ref = oauth_token
        WHERE oauth_token IS NOT NULL AND secret_blob_ref IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE maxplanaccount
        SET oauth_token = ''
        WHERE oauth_token IS NULL
        """
    )
    op.alter_column("maxplanaccount", "oauth_token", existing_type=sa.String(), nullable=False)
    op.drop_column("maxplanaccount", "provider_metadata")
    op.drop_column("maxplanaccount", "secret_blob_ref")
    op.drop_column("maxplanaccount", "auth_mode")
    op.drop_column("maxplanaccount", "provider")

    bind = op.get_bind()
    auth_mode_enum.drop(bind, checkfirst=True)
    provider_enum.drop(bind, checkfirst=True)
