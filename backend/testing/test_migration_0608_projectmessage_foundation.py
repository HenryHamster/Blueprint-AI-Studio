"""Focused tests for ProjectMessage schema/backfill revision 0608."""

import ast
import importlib.util
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models import (
    MessageHandlingState,
    MessageSurface,
    ProjectMessage,
    ProjectMessageAttachmentUpload,
    ProjectMessageSequence,
)


def _migration_path():
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    return next(iter(versions.glob("0608_*.py")))


def _load_migration():
    path = _migration_path()
    spec = importlib.util.spec_from_file_location("migration_0608_messages", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(*, mid, project, created, **overrides):
    row = {
        "id": mid,
        "project_id": project,
        "created_at": created,
        "resolved_at": None,
        "from_type": "system",
        "message_type": "NOTE",
        "status": "open",
        "context": {},
        "from_task_id": None,
    }
    row.update(overrides)
    return row


def test_backfill_is_deterministic_and_project_scoped():
    mig = _load_migration()
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    t0 = datetime(2026, 7, 15, 12)
    low = uuid.UUID("00000000-0000-0000-0000-000000000001")
    high = uuid.UUID("00000000-0000-0000-0000-000000000002")
    other = uuid.UUID("00000000-0000-0000-0000-000000000003")

    # Deliberately unordered input; equal timestamps use message UUID as the
    # stable tiebreaker and each project starts at sequence 1.
    values = mig._build_backfill_values(
        [
            _row(mid=high, project=p1, created=t0),
            _row(mid=other, project=p2, created=t0),
            _row(mid=low, project=p1, created=t0),
        ]
    )
    by_id = {value["row_id"]: value for value in values}
    assert by_id[low]["sequence"] == 1
    assert by_id[high]["sequence"] == 2
    assert by_id[other]["sequence"] == 1


def test_restartable_sequence_assignment_appends_after_partial_prefix():
    mig = _load_migration()
    project = uuid.uuid4()
    t0 = datetime(2026, 7, 15, 12)
    committed = uuid.uuid4()
    remaining_a = uuid.uuid4()
    remaining_b = uuid.uuid4()
    backdated_insert = uuid.uuid4()
    reserved_insert = uuid.uuid4()

    # A prior batch persisted sequence 2, then the original sequence-1 row was
    # deleted and concurrent inserts arrived. Retry appends NULL rows after the
    # committed low prefix and ignores the compatibility-reserved range.
    assigned = mig._build_restartable_sequence_values(
        [
            _row(
                mid=committed,
                project=project,
                created=t0 + timedelta(seconds=1),
                sequence=2,
            ),
            _row(
                mid=remaining_a,
                project=project,
                created=t0 + timedelta(seconds=2),
                sequence=None,
            ),
            _row(
                mid=remaining_b,
                project=project,
                created=t0 + timedelta(seconds=3),
                sequence=None,
            ),
            _row(
                mid=backdated_insert,
                project=project,
                created=t0 - timedelta(seconds=1),
                sequence=None,
            ),
            _row(
                mid=reserved_insert,
                project=project,
                created=t0 - timedelta(seconds=2),
                sequence=mig._COMPAT_SEQUENCE_FLOOR,
            ),
        ]
    )

    assert assigned == {
        backdated_insert: 3,
        remaining_a: 4,
        remaining_b: 5,
    }
    assert max(assigned.values()) < mig._COMPAT_SEQUENCE_FLOOR

    with pytest.raises(ValueError, match="historical sequence range exhausted"):
        mig._build_restartable_sequence_values(
            [
                _row(
                    mid=committed,
                    project=project,
                    created=t0,
                    sequence=mig._COMPAT_SEQUENCE_FLOOR - 1,
                ),
                _row(
                    mid=remaining_a,
                    project=project,
                    created=t0 + timedelta(seconds=1),
                    sequence=None,
                ),
            ]
        )


def test_backfill_projects_surface_actor_reply_and_handling_conservatively():
    mig = _load_migration()
    project, foreign_project = uuid.uuid4(), uuid.uuid4()
    target, reply, routine, blocker, foreign, feedback, decision, linked = [
        uuid.uuid4() for _ in range(8)
    ]
    handling_task = uuid.uuid4()
    t0 = datetime(2026, 7, 15, 12)

    values = mig._build_backfill_values(
        [
            _row(
                mid=target,
                project=project,
                created=t0,
                from_type="user",
                context={"operator_user_id": " operator-1 "},
            ),
            _row(
                mid=reply,
                project=project,
                created=t0 + timedelta(seconds=1),
                from_type="strategic",
                context={"reply_to": str(target)},
                from_task_id=handling_task,
            ),
            _row(mid=routine, project=project, created=t0 + timedelta(seconds=2)),
            _row(
                mid=blocker,
                project=project,
                created=t0 + timedelta(seconds=3),
                message_type="BLOCKER",
                context={"check_in": {"operator_user_id": "operator-2"}},
            ),
            _row(
                mid=foreign,
                project=foreign_project,
                created=t0,
                context={"reply_to": str(target)},
            ),
            _row(
                mid=feedback,
                project=project,
                created=t0 + timedelta(seconds=4),
                from_type="user",
                message_type="FEEDBACK",
            ),
            _row(
                mid=decision,
                project=project,
                created=t0 + timedelta(seconds=5),
                from_type="system",
                context={"event_type": "remediation_escalated"},
            ),
            _row(
                mid=linked,
                project=project,
                created=t0 + timedelta(seconds=6),
                from_type="user",
                status="linked",
            ),
        ]
    )
    by_id = {value["row_id"]: value for value in values}

    assert by_id[target]["actor_user_id"] == "operator-1"
    assert by_id[target]["surface"] == "conversation"
    assert by_id[target]["handling_state"] == "responded"
    assert by_id[target]["handled_by_task_id"] == handling_task
    assert by_id[reply]["reply_to_message_id"] == target
    assert by_id[reply]["handling_state"] == "responded"
    assert by_id[routine]["surface"] == "activity"
    assert by_id[routine]["handling_state"] == "pending"
    assert by_id[blocker]["surface"] == "activity"
    assert by_id[blocker]["handling_state"] == "pending"
    assert by_id[blocker]["actor_user_id"] == "operator-2"
    assert by_id[foreign]["reply_to_message_id"] is None
    assert by_id[foreign]["surface"] == "activity"
    assert by_id[feedback]["surface"] == "conversation"
    assert by_id[feedback]["handling_state"] == "pending"
    assert by_id[decision]["surface"] == "conversation"
    assert by_id[decision]["handling_state"] == "pending"
    assert by_id[linked]["handling_state"] == "pending"


def test_portable_backfill_rejects_cross_project_handler_attribution():
    mig = _load_migration()
    project, foreign_project = uuid.uuid4(), uuid.uuid4()
    target, reply, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    t0 = datetime(2026, 7, 15, 12)

    values = mig._build_backfill_values(
        [
            _row(
                mid=target,
                project=project,
                created=t0,
                from_type="user",
            ),
            _row(
                mid=reply,
                project=project,
                created=t0 + timedelta(seconds=1),
                from_type="strategic",
                from_task_id=task_id,
                context={"reply_to": str(target)},
            ),
        ],
        task_project_by_id={task_id: foreign_project},
    )

    target_value = next(value for value in values if value["row_id"] == target)
    assert target_value["handling_state"] == "responded"
    assert target_value["handled_by_task_id"] is None


def test_projectmessage_model_exposes_rollout_contract():
    table = ProjectMessage.__table__
    assert ProjectMessage().surface == MessageSurface.ACTIVITY
    assert ProjectMessage().handling_state == MessageHandlingState.PENDING
    assert table.c.sequence.nullable is True
    assert table.c.updated_at.nullable is False
    assert next(iter(table.c.handled_by_task_id.foreign_keys)).ondelete == "SET NULL"
    assert next(iter(table.c.reply_to_message_id.foreign_keys)).ondelete == "SET NULL"
    index_names = {index.name for index in table.indexes}
    assert {
        "ux_projectmessage_project_sequence",
        "ix_projectmessage_project_surface_sequence",
        "ix_projectmessage_project_handling_sequence",
        "ix_projectmessage_actor_user_id",
        "ix_projectmessage_handled_by_task_id",
        "ix_projectmessage_reply_to_message_id",
    } <= index_names


def test_sequence_and_attachment_receipt_models_expose_rollout_contract():
    sequence = ProjectMessageSequence.__table__
    assert sequence.c.project_id.primary_key is True
    assert sequence.c.last_sequence.nullable is False
    assert next(iter(sequence.c.project_id.foreign_keys)).ondelete == "CASCADE"
    assert {
        constraint.name for constraint in sequence.constraints
    } >= {"chk_projectmessagesequence_nonnegative"}

    receipt = ProjectMessageAttachmentUpload.__table__
    assert receipt.c.attachment_id.primary_key is True
    assert receipt.c.project_id.nullable is False
    assert receipt.c.actor_user_id.nullable is False
    assert receipt.c.expected_size_bytes.nullable is False
    assert receipt.c.consumed_at.nullable is True
    assert receipt.c.message_id.nullable is True
    assert next(iter(receipt.c.project_id.foreign_keys)).ondelete == "CASCADE"
    assert next(iter(receipt.c.message_id.foreign_keys)).ondelete == "SET NULL"
    assert {
        "ix_projectmessageattachmentupload_project_expires",
        "ix_projectmessageattachmentupload_project_actor_created",
        "ix_projectmessageattachmentupload_message_id",
        "ix_projectmessageattachmentupload_consumed_expires",
    } <= {index.name for index in receipt.indexes}


def test_postgresql_rollout_shape_is_low_lock_and_bounded():
    source = _migration_path().read_text(encoding="utf-8")

    assert "autocommit_block" in source
    assert "CREATE TEMP TABLE projectmessage_0567_backfill" in source
    assert "LIMIT :batch_size" in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "index_state.indisvalid" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    assert "NOT VALID" in source
    assert "VALIDATE CONSTRAINT" in source
    assert "CREATE CONSTRAINT TRIGGER trg_projectmessage_project_links" in source
    assert "CREATE CONSTRAINT TRIGGER trg_task_projectmessage_links" in source
    assert "BEFORE INSERT OR UPDATE OF project_id" in source
    assert "FOR SHARE" in source
    assert "projectmessagesequence" in source
    assert "ON CONFLICT (project_id) DO UPDATE" in source
    assert "allocator.last_sequence = NEW.sequence" in source
    assert "_COMPAT_SEQUENCE_FLOOR = 1_000_000_000_000" in source
    assert "NEW.sequence IS NOT NULL" in source
    assert "WHERE m.sequence IS NULL" in source
    assert "max(sequence) FILTER" in source
    assert "FROM projectmessage AS reply" in source
    assert "_reconcile_postgresql_reply_handling()" in source
    seed_body = source.split("def _seed_sequence_counters", 1)[1].split(
        "def _add_columns", 1
    )[0]
    assert "JOIN projectmessage" not in seed_body
    assert "SELECT project.id" in seed_body
    # The PostgreSQL path must not materialize the SELECT result in Python.
    postgres_body = source.split("def _postgresql_backfill()", 1)[1].split(
        "def _portable_backfill()", 1
    )[0]
    assert "list(" not in postgres_body
    assert "fetchall" not in postgres_body
    loop_body = postgres_body.split("while True:", 1)[1].split(
        'op.execute("DROP TABLE projectmessage_0567_backfill")', 1
    )[0]
    assert "DELETE FROM projectmessage_0567_backfill" in loop_body
    assert "RETURNING *" in loop_body
    assert "JOIN projectmessage_0567_backfill" not in loop_body
    assert "ORDER BY project_id, sequence, row_id" in loop_body
    assert "SELECT EXISTS (SELECT 1 FROM projectmessage_0567_backfill LIMIT 1)" in loop_body
    assert "rowcount" not in loop_body

    compat_body = source.split(
        "def _install_postgresql_compatibility_trigger()", 1
    )[1].split("def _postgresql_backfill()", 1)[0]
    invariant_position = compat_body.index(
        "reply message must belong to the same project"
    )
    allocator_section = compat_body.index("should_project :=")
    assert invariant_position < allocator_section


def test_production_message_writers_use_canonical_sequence_builder():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    violations: list[str] = []
    allowed_direct = {
        app_dir / "project_message_creation.py",
        # The pure onboarding seed receives an allocator value from its async
        # production caller and explicitly supplies the durable fields.
        app_dir / "routers" / "admin_onboard.py",
    }
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name != "ProjectMessage":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            if path in allowed_direct:
                if path.name == "admin_onboard.py" and not {
                    "sequence",
                    "surface",
                    "handling_state",
                } <= set(keywords):
                    violations.append(f"{path.relative_to(app_dir)}:{node.lineno}")
                continue
            violations.append(f"{path.relative_to(app_dir)}:{node.lineno}")
    assert not violations, (
        "production ProjectMessage writers must use build_project_message; "
        f"the migration trigger is only a mixed-version backstop: {violations}"
    )


def test_message_mutation_sources_follow_project_first_lock_order():
    # Deadlock-safe canonical order is PARENT-first: lock the Project row, then
    # reserve the message counter, then lock the Task (parent → counter → child).
    # A consistent parent-first hierarchy across every mutation source is what
    # prevents ABBA deadlocks; the message-counter helper itself locks the
    # project before the counter.
    app_dir = Path(__file__).resolve().parents[1] / "app"
    agent_source = (
        app_dir / "routers" / "admin_agent_ops.py"
    ).read_text(encoding="utf-8")
    strategic = agent_source.split("async def strategic_operation(", 1)[1]
    preallocate = strategic.index("preallocated_message_sequence")
    project_lock = strategic.index("_lock_project_or_429")
    task_lock_position = strategic.index("async with task_lock")
    assert project_lock < preallocate < task_lock_position

    operator_source = (
        app_dir / "routers" / "admin_notifications.py"
    ).read_text(encoding="utf-8")
    resolve = operator_source.split("async def resolve_message(", 1)[1]
    counter = resolve.index("lock_project_and_next_message_sequence")
    project_row = resolve.index("select(Project.id)")
    reply_row = resolve.index("select(ProjectMessage)")
    assert project_row < counter < reply_row

    migration_source = _migration_path().read_text(encoding="utf-8")
    compat = migration_source.split("should_project :=", 1)[1].split(
        "IF should_project", 1
    )[0]
    assert "NEW.sequence IS NULL" in compat


def _sqlite_legacy_schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project", metadata, sa.Column("id", sa.Uuid(), primary_key=True)
    )
    task = sa.Table(
        "task",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
    )
    message = sa.Table(
        "projectmessage",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("from_type", sa.String(), nullable=False),
        sa.Column("message_type", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("context", sa.JSON()),
        sa.Column("from_task_id", sa.Uuid()),
        sa.Column("content", sa.String(), nullable=False, server_default=""),
    )
    metadata.create_all(connection)
    return project, task, message


def test_sqlite_upgrade_enforces_same_project_links_and_downgrades():
    mig = _load_migration()
    engine = sa.create_engine("sqlite://")
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    task1, task2 = uuid.uuid4(), uuid.uuid4()
    target = uuid.uuid4()
    legacy_reply = uuid.uuid4()
    with engine.begin() as connection:
        project, task, message = _sqlite_legacy_schema(connection)
        connection.execute(project.insert(), [{"id": p1}, {"id": p2}])
        connection.execute(
            task.insert(),
            [
                {"id": task1, "project_id": p1},
                {"id": task2, "project_id": p2},
            ],
        )
        connection.execute(
            message.insert().values(
                id=target,
                project_id=p1,
                created_at=datetime(2026, 7, 15, 12),
                from_type="user",
                message_type="QUESTION",
                status="open",
                context={},
                content="target",
            )
        )
        connection.execute(
            message.insert().values(
                id=legacy_reply,
                project_id=p1,
                created_at=datetime(2026, 7, 15, 12, 1),
                from_type="strategic",
                from_task_id=task2,
                message_type="NOTE",
                status="open",
                context={"reply_to": str(target)},
                content="legacy reply with a foreign task",
            )
        )
        original_op = mig.op
        mig.op = Operations(MigrationContext.configure(connection))
        try:
            mig.upgrade()
            upgraded = sa.Table(
                "projectmessage", sa.MetaData(), autoload_with=connection
            )
            sequence_table = sa.Table(
                "projectmessagesequence", sa.MetaData(), autoload_with=connection
            )
            receipt_table = sa.Table(
                "projectmessageattachmentupload",
                sa.MetaData(),
                autoload_with=connection,
            )
            target_row = connection.execute(
                sa.select(
                    upgraded.c.handling_state,
                    upgraded.c.handled_by_task_id,
                ).where(upgraded.c.id == target.hex)
            ).one()
            assert target_row.handling_state == "responded"
            assert target_row.handled_by_task_id is None
            assert connection.scalar(
                sa.select(sequence_table.c.last_sequence).where(
                    sequence_table.c.project_id == p1.hex
                )
            ) == mig._COMPAT_SEQUENCE_FLOOR - 1
            valid_id = uuid.uuid4().hex
            valid = {
                "id": valid_id,
                "project_id": p1.hex,
                "created_at": datetime(2026, 7, 15, 13),
                "from_type": "strategic",
                "message_type": "NOTE",
                "status": "open",
                "context": {},
                "content": "reply",
                "reply_to_message_id": target.hex,
                "handled_by_task_id": task1.hex,
            }
            connection.execute(upgraded.insert().values(**valid))
            valid_row = connection.execute(
                sa.select(
                    upgraded.c.sequence,
                    upgraded.c.surface,
                    upgraded.c.handling_state,
                ).where(upgraded.c.id == valid_id)
            ).one()
            assert valid_row == (
                mig._COMPAT_SEQUENCE_FLOOR,
                "conversation",
                "responded",
            )
            assert connection.scalar(
                sa.select(sequence_table.c.last_sequence).where(
                    sequence_table.c.project_id == p1.hex
                )
            ) == mig._COMPAT_SEQUENCE_FLOOR

            p3 = uuid.uuid4()
            connection.execute(project.insert().values(id=p3))
            first_new_project_message = uuid.uuid4().hex
            connection.execute(
                upgraded.insert().values(
                    id=first_new_project_message,
                    project_id=p3.hex,
                    created_at=datetime(2026, 7, 15, 14),
                    from_type="user",
                    message_type="NOTE",
                    status="open",
                    context={"operator_user_id": "operator-3"},
                    content="first new-project conversation message",
                )
            )
            new_project_row = connection.execute(
                sa.select(
                    upgraded.c.sequence,
                    upgraded.c.surface,
                    upgraded.c.actor_user_id,
                ).where(upgraded.c.id == first_new_project_message)
            ).one()
            assert new_project_row == (1, "conversation", "operator-3")

            promoted_activity = uuid.uuid4().hex
            connection.execute(
                upgraded.insert().values(
                    id=promoted_activity,
                    project_id=p3.hex,
                    created_at=datetime(2026, 7, 15, 14, 1),
                    from_type="system",
                    message_type="NOTE",
                    status="open",
                    context={},
                    content="promote me later",
                )
            )
            connection.execute(
                upgraded.update()
                .where(upgraded.c.id == promoted_activity)
                .values(context={"event_type": "remediation_escalated"})
            )
            assert connection.execute(
                sa.select(upgraded.c.sequence, upgraded.c.surface).where(
                    upgraded.c.id == promoted_activity
                )
            ).one() == (2, "conversation")

            receipt_id = uuid.uuid4().hex
            receipt_created = datetime(2026, 7, 15, 15)
            connection.execute(
                receipt_table.insert().values(
                    attachment_id=receipt_id,
                    project_id=p1.hex,
                    actor_user_id="operator-1",
                    storage_uri=f"gs://bucket/{p1}/{receipt_id}",
                    filename="evidence.txt",
                    content_type="text/plain",
                    expected_size_bytes=12,
                    expires_at=receipt_created + timedelta(minutes=15),
                    created_at=receipt_created,
                )
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    receipt_table.insert().values(
                        attachment_id=uuid.uuid4().hex,
                        project_id=p1.hex,
                        actor_user_id="operator-1",
                        storage_uri="gs://bucket/invalid",
                        filename="invalid.txt",
                        expected_size_bytes=-1,
                        expires_at=receipt_created + timedelta(minutes=15),
                        created_at=receipt_created,
                    )
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    upgraded.update()
                    .where(upgraded.c.id == target.hex)
                    .values(project_id=p2.hex)
                )
            reflected_task = sa.Table("task", sa.MetaData(), autoload_with=connection)
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    reflected_task.update()
                    .where(reflected_task.c.id == task1.hex)
                    .values(project_id=p2.hex)
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    upgraded.insert().values(
                        **{
                            **valid,
                            "id": uuid.uuid4().hex,
                            "project_id": p2.hex,
                            "sequence": 1_000_000_000_000,
                            "content": "cross-project reply",
                        }
                    )
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    upgraded.insert().values(
                        **{
                            **valid,
                            "id": uuid.uuid4().hex,
                            "sequence": 1_000_000_000_001,
                            "reply_to_message_id": None,
                            "handled_by_task_id": task2.hex,
                            "content": "cross-project task",
                        }
                    )
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    upgraded.update()
                    .where(upgraded.c.id == valid_id)
                    .values(project_id=p2.hex)
                )
            mig.downgrade()
            columns = {column["name"] for column in sa.inspect(connection).get_columns("projectmessage")}
            assert "sequence" not in columns
            assert "reply_to_message_id" not in columns
            assert not sa.inspect(connection).has_table("projectmessagesequence")
            assert not sa.inspect(connection).has_table(
                "projectmessageattachmentupload"
            )
        finally:
            mig.op = original_op
