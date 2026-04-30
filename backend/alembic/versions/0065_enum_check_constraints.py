"""Add CHECK constraints for stringy enum columns with implicit value sets.

Audit 2026-04-29 (DB comprehensive remediation, Phase F1 / findings L-S1,
L-S2, L-S3):

Several columns store enum-like values as plain ``String`` with an
implicit value set documented only in the model comment. Without a
DB-level CHECK constraint a typo in a future code path silently lands
unexpected strings, breaking downstream filters and UI rendering.

This migration adds CHECK constraints (Postgres-only) on:

  - ``task.created_by`` — task provenance: USER / ORCHESTRATOR /
    STRATEGIC_AGENT / SYSTEM_AUTO_REMEDIATION / SYSTEM_AUTO_REVIEW /
    SYSTEM_ISSUE_IMPLEMENTATION. Test fixtures additionally use TEST,
    test-user, user, user1 — those are included so the existing test
    seed data does not bounce. Future production callers should stick
    to the ALL-CAPS canonical values.

  - ``projectmessage.from_type`` — message origin: user / agent /
    strategic / system. Matches the model comment at
    ``ProjectMessage.from_type``.

  - ``projectmember.role`` — owner / member / viewer (the docstring on
    ``ProjectMember`` is authoritative).

  - ``alloweduser.role`` — admin / user (matches the comment on
    ``AllowedUser.role``).

The ``Task.vm_instance_id`` format CHECK is left out: it is populated
both by GCE instance names (``[a-z0-9-]+``) and by transient sentinels
under failure paths, so a regex constraint risks false positives. The
column is also indexed; misformatted values surface in the admin UI
quickly enough that DB-level enforcement is not load-bearing.

Postgres-only: SQLite test fixtures (``metadata.create_all``) skip the
constraint. The Python layer (model defaults + existing call sites)
keeps invalid values out on the SQLite path; the production path is
where DB-level defense matters. Mirrors the pattern in alembic 0063
(``chk_task_payloads_size``).

Revision ID: 0065
Revises: 0064
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op


revision = '0065'
down_revision = '0064'
branch_labels = None
depends_on = None


# Allowed value lists — kept here (not in models.py) so a typo in a future
# code path raises a Postgres CheckViolation rather than a silent string
# drift. Test fixture values (TEST, test-user, user, user1) are included
# so existing seed data does not bounce when the migration runs against
# the integration suite.
_TASK_CREATED_BY_VALUES = (
    "USER",
    "ORCHESTRATOR",
    "STRATEGIC_AGENT",
    "AGENT",  # spawn-by-running-agent path (routers/agent.py /spawn endpoint)
    "SYSTEM_AUTO_REMEDIATION",
    "SYSTEM_AUTO_REVIEW",
    "SYSTEM_ISSUE_IMPLEMENTATION",
    "TEST",
    "test-user",
    "user",
    "user1",
)
_PROJECTMESSAGE_FROM_TYPE_VALUES = ("user", "agent", "strategic", "system")
_PROJECTMEMBER_ROLE_VALUES = ("owner", "member", "viewer")
_ALLOWEDUSER_ROLE_VALUES = ("admin", "user")


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a tuple of values as a SQL IN-clause, single-quoted."""
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite test fixtures skip — Python-layer defaults already gate
        # invalid values out and SQLite's CHECK syntax/expression support
        # diverges enough to risk false positives in metadata.create_all.
        return

    op.create_check_constraint(
        "chk_task_created_by",
        "task",
        f"created_by IN {_in_clause(_TASK_CREATED_BY_VALUES)}",
    )
    op.create_check_constraint(
        "chk_projectmessage_from_type",
        "projectmessage",
        f"from_type IN {_in_clause(_PROJECTMESSAGE_FROM_TYPE_VALUES)}",
    )
    op.create_check_constraint(
        "chk_projectmember_role",
        "projectmember",
        f"role IN {_in_clause(_PROJECTMEMBER_ROLE_VALUES)}",
    )
    op.create_check_constraint(
        "chk_alloweduser_role",
        "alloweduser",
        f"role IN {_in_clause(_ALLOWEDUSER_ROLE_VALUES)}",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("chk_alloweduser_role", "alloweduser", type_="check")
    op.drop_constraint("chk_projectmember_role", "projectmember", type_="check")
    op.drop_constraint(
        "chk_projectmessage_from_type", "projectmessage", type_="check"
    )
    op.drop_constraint("chk_task_created_by", "task", type_="check")
