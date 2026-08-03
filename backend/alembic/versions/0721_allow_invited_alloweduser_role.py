"""Allow internal invitation provenance in AllowedUser.role.

Revision ID: 0721
Revises: 0720
Create Date: 2026-08-02

``invited`` distinguishes an allowlist row created solely by a project
invitation from a durable administrator-created ``user`` row.  That provenance
lets cancellation remove only disposable pre-login rows.

This constraint-only migration adds no ``nullable=False`` column, so no
``server_default`` is required.
"""

from alembic import op


revision = "0721"
down_revision = "0720"
branch_labels = None
depends_on = None


def _replace_role_constraint(values: tuple[str, ...]) -> None:
    op.drop_constraint("chk_alloweduser_role", "alloweduser", type_="check")
    rendered = ",".join(f"'{value}'" for value in values)
    op.create_check_constraint(
        "chk_alloweduser_role", "alloweduser", f"role IN ({rendered})"
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _replace_role_constraint(("admin", "user", "invited"))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("UPDATE alloweduser SET role = 'user' WHERE role = 'invited'")
    _replace_role_constraint(("admin", "user"))
