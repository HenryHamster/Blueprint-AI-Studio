"""Focused schema, backfill, and fencing tests for revision 0611."""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url

from app.migration_apply_gate import ephemeral_postgres_database
from app.models import (
    StrategicOperationReceipt,
    StrategicResident,
    StrategicResidentCheckpoint,
)

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")
requires_postgres = pytest.mark.skipif(
    not USE_POSTGRES,
    reason="Strategic Resident migration trigger smoke test requires Postgres",
)


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0611_strategic_resident_control_plane.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0611_resident", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_initial_checkpoint_is_deterministic_bounded_and_drops_operational_keys():
    migration = _load_migration()
    project = {
        "problem_model": {
            "purpose": "Help operators ship",
            "_cache": {"task_ids": ["secret-operational-history"]},
        },
        "vision": {
            "purpose": "Durable strategy",
            "_resident_strategic_session": {
                "_exam_ledger": "raw ledger",
                "_message_cursor": "opaque-timestamp-cursor",
                "_session_summary": "Settled strategy summary",
                "_decisions_reg": json.dumps(
                    {
                        "version": 1,
                        "decisions": [
                            {"id": "decision-1", "summary": "Keep chat calm"}
                        ],
                    }
                ),
            },
        },
        "directions": [
            {
                "id": "d-1",
                "text": "Ship the durable Resident",
                "status": "active",
                "_task_history": ["task-1"],
            },
            {"id": "d-old", "text": "Done", "status": "completed"},
        ],
        "strategic_memory_notes": [
            {"note": "Keep the chat calm", "set_at": "2026-07-16T00:00:00"}
        ],
        "running_update": "The durable Resident is being introduced.",
    }

    first, first_hash = migration._build_initial_checkpoint(project)
    second, second_hash = migration._build_initial_checkpoint(project)

    assert first == second
    assert first_hash == second_hash
    assert len(first_hash) == 64
    assert len(migration._canonical_json(first)) <= migration._CHECKPOINT_MAX_BYTES
    serialized = json.dumps(first, sort_keys=True)
    assert "_resident_strategic_session" not in serialized
    assert "_exam_ledger" not in serialized
    assert "secret-operational-history" not in serialized
    assert (
        first["working_strategy"]["legacy_session_summary"]
        == "Settled strategy summary"
    )
    assert first["working_strategy"]["legacy_decisions_register"] == {
        "version": 1,
        "decisions": [{"id": "decision-1", "summary": "Keep chat calm"}],
    }
    assert first["cursors"]["legacy_message_cursor"] == "opaque-timestamp-cursor"
    assert [item["id"] for item in first["working_strategy"]["directions"]] == ["d-1"]
    assert first["cursors"]["handled_message_sequence"] == 0


def test_initial_checkpoint_has_a_hard_node_and_byte_budget():
    migration = _load_migration()
    nested: dict[str, object] = {}
    cursor = nested
    for index in range(20):
        next_level = {f"key-{index}-{child}": "x" * 20_000 for child in range(400)}
        cursor[f"level-{index}"] = next_level
        cursor = next_level

    checkpoint, content_hash = migration._build_initial_checkpoint(
        {
            "problem_model": nested,
            "vision": nested,
            "directions": [],
            "strategic_memory_notes": [],
        }
    )

    assert len(migration._canonical_json(checkpoint)) <= migration._CHECKPOINT_MAX_BYTES
    assert len(content_hash) == 64
    assert checkpoint["migration"]["truncated"] is True


@pytest.mark.parametrize(
    ("paused", "continuous", "chat", "expected"),
    [
        (False, False, False, "active"),
        (False, False, True, "paused"),
        (False, True, False, "active"),
        (False, True, True, "active"),
        (True, True, False, "paused"),
    ],
)
def test_background_policy_backfill_rule(paused, continuous, chat, expected):
    migration = _load_migration()
    assert (
        migration._background_policy_for(
            project_paused=paused,
            has_continuous=continuous,
            has_chat=chat,
        )
        == expected
    )


def test_resident_models_expose_authority_checkpoint_and_receipt_contract():
    resident = StrategicResident.__table__
    checkpoint = StrategicResidentCheckpoint.__table__
    receipt = StrategicOperationReceipt.__table__

    assert resident.c.project_id.primary_key
    assert resident.c.authority_generation.nullable is False
    assert resident.c.active_turn_task_id.nullable is True
    assert resident.c.active_turn_lane.nullable is True
    assert resident.c.interactive_successor_task_id.nullable is True
    assert resident.c.background_successor_task_id.nullable is True
    assert next(iter(resident.c.project_id.foreign_keys)).ondelete == "CASCADE"
    current_fk = next(iter(resident.c.current_checkpoint_id.foreign_keys))
    previous_fk = next(iter(resident.c.previous_checkpoint_id.foreign_keys))
    assert current_fk.ondelete == previous_fk.ondelete == "SET NULL"
    assert current_fk.use_alter is True
    assert previous_fk.use_alter is True
    assert {
        "ix_strategicresident_scheduler",
        "ix_strategicresident_owner_health",
        "ix_strategicresident_active_turn_task",
        "ix_strategicresident_interactive_successor",
        "ix_strategicresident_background_successor",
    } <= {index.name for index in resident.indexes}

    assert checkpoint.c.version.nullable is False
    assert checkpoint.c.content_hash.type.length == 64
    assert next(iter(checkpoint.c.project_id.foreign_keys)).ondelete == "CASCADE"
    assert not checkpoint.c.source_turn_task_id.foreign_keys
    assert {constraint.name for constraint in checkpoint.constraints} >= {
        "uq_strategicresidentcheckpoint_project_version"
    }

    assert receipt.c.turn_task_id.nullable is False
    assert not receipt.c.turn_task_id.foreign_keys
    assert receipt.c.capability.nullable is False
    assert receipt.c.error_detail.type.length == 1000
    assert {
        "ix_strategicoperationreceipt_project_status_updated",
        "ix_strategicoperationreceipt_task_started",
    } <= {index.name for index in receipt.indexes}


def _sqlite_legacy_schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("vision", sa.JSON()),
        sa.Column("problem_model", sa.JSON()),
        sa.Column("directions", sa.JSON()),
        sa.Column("strategic_memory_notes", sa.JSON()),
        sa.Column("running_update", sa.Text()),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    task = sa.Table(
        "task",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("payloads", sa.JSON(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
    )
    prefs = sa.Table(
        "messagesnoiseprefs",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("prefs", sa.JSON(), nullable=False),
    )
    message = sa.Table(
        "projectmessage",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger()),
        sa.Column("surface", sa.String(32), nullable=False),
        sa.Column("handling_state", sa.String(32), nullable=False),
    )
    metadata.create_all(connection)
    return project, task, prefs, message


def test_sqlite_upgrade_backfills_and_fences_authority_and_project_links():
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    task1, task2 = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        project, task, prefs, message = _sqlite_legacy_schema(connection)
        connection.execute(
            project.insert(),
            [
                {
                    "id": p1,
                    "vision": {
                        "purpose": "one",
                        "_resident_strategic_session": {
                            "session_id": "ss-one",
                            "task_id": str(task1),
                            "_exam_ledger": "raw",
                            "_session_summary": "Legacy settled strategy",
                            "_decisions_reg": json.dumps(
                                {
                                    "version": 1,
                                    "decisions": [
                                        {
                                            "id": "legacy-decision",
                                            "summary": "Preserve this decision",
                                        }
                                    ],
                                }
                            ),
                            "_message_cursor": "legacy-cursor",
                        },
                    },
                    "problem_model": {"purpose": "charter one", "_cache": "drop"},
                    "directions": [{"id": "d-1", "text": "Active", "status": "active"}],
                    "strategic_memory_notes": [{"note": "remember one"}],
                    "running_update": "one update",
                    "paused": False,
                },
                {
                    "id": p2,
                    "vision": {"purpose": "two"},
                    "problem_model": {"purpose": "charter two"},
                    "directions": [],
                    "strategic_memory_notes": [],
                    "running_update": None,
                    "paused": True,
                },
            ],
        )
        connection.execute(
            task.insert(),
            [
                {
                    "id": task1,
                    "project_id": p1,
                    "status": "IN_PROGRESS",
                    "payloads": [{"type": "strategic_resident_turn"}],
                    "context": {"_resident_mutation_authorized": True},
                },
                {
                    "id": task2,
                    "project_id": p2,
                    "status": "IN_PROGRESS",
                    "payloads": [{"type": "strategic_resident_turn"}],
                    "context": {"_resident_mutation_authorized": True},
                },
            ],
        )
        connection.execute(
            prefs.insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "project_id": p1,
                    "prefs": {"_strategic_workbench": {"interaction_mode": "chat"}},
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": p1,
                    "prefs": {
                        "_strategic_workbench": {"interaction_mode": "continuous"}
                    },
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": p2,
                    "prefs": {
                        "_strategic_workbench": {"interaction_mode": "continuous"}
                    },
                },
            ],
        )
        connection.execute(
            message.insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "project_id": p1,
                    "sequence": 1,
                    "surface": "conversation",
                    "handling_state": "responded",
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": p1,
                    "sequence": 2,
                    "surface": "activity",
                    "handling_state": "pending",
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": p1,
                    "sequence": 3,
                    "surface": "conversation",
                    "handling_state": "responded",
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": p1,
                    "sequence": 5,
                    "surface": "conversation",
                    "handling_state": "pending",
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": p2,
                    "sequence": 10,
                    "surface": "conversation",
                    "handling_state": "responded",
                },
            ],
        )

        original_op = migration.op
        migration.op = Operations(MigrationContext.configure(connection))
        try:
            migration.upgrade()
            resident = sa.Table(
                "strategicresident", sa.MetaData(), autoload_with=connection
            )
            checkpoint = sa.Table(
                "strategicresidentcheckpoint",
                sa.MetaData(),
                autoload_with=connection,
            )
            receipt = sa.Table(
                "strategicoperationreceipt",
                sa.MetaData(),
                autoload_with=connection,
            )

            resident_row = (
                connection.execute(
                    sa.select(resident).where(resident.c.project_id == p1.hex)
                )
                .mappings()
                .one()
            )
            assert resident_row["engine_owner"] == "legacy"
            assert resident_row["background_policy"] == "active"
            assert resident_row["authority_generation"] == 1
            assert resident_row["handled_message_sequence"] == 4
            assert resident_row["active_turn_task_id"] is None
            assert resident_row["current_checkpoint_id"] is not None
            assert resident_row["previous_checkpoint_id"] is None
            assert resident_row["rollout_state"]["legacy_session_id"] == "ss-one"
            assert (
                connection.scalar(
                    sa.select(resident.c.background_policy).where(
                        resident.c.project_id == p2.hex
                    )
                )
                == "paused"
            )

            checkpoint_row = (
                connection.execute(
                    sa.select(checkpoint).where(checkpoint.c.project_id == p1.hex)
                )
                .mappings()
                .one()
            )
            assert checkpoint_row["version"] == 1
            assert len(checkpoint_row["content_hash"]) == 64
            serialized = json.dumps(checkpoint_row["checkpoint"], sort_keys=True)
            assert "_resident_strategic_session" not in serialized
            assert "_exam_ledger" not in serialized
            assert "_cache" not in serialized
            assert (
                checkpoint_row["checkpoint"]["working_strategy"][
                    "legacy_session_summary"
                ]
                == "Legacy settled strategy"
            )
            assert (
                checkpoint_row["checkpoint"]["working_strategy"][
                    "legacy_decisions_register"
                ]["decisions"][0]["id"]
                == "legacy-decision"
            )
            assert (
                checkpoint_row["checkpoint"]["cursors"]["legacy_message_cursor"]
                == "legacy-cursor"
            )
            assert (
                checkpoint_row["checkpoint"]["cursors"]["handled_message_sequence"] == 4
            )
            assert (
                connection.scalar(
                    sa.select(resident.c.handled_message_sequence).where(
                        resident.c.project_id == p2.hex
                    )
                )
                == 10
            )

            # Checkpoints are immutable, while a restart adopts the unchanged
            # existing v1 row without duplicating or rewriting it.
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    checkpoint.update()
                    .where(checkpoint.c.project_id == p1.hex)
                    .values(content_hash="f" * 64)
                )
            original_checkpoint_id = checkpoint_row["id"]
            migration._backfill(postgresql_dialect=False)
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(checkpoint))
                == 2
            )
            assert (
                connection.scalar(
                    sa.select(resident.c.current_checkpoint_id).where(
                        resident.c.project_id == p1.hex
                    )
                )
                == original_checkpoint_id
            )

            # Cross-project task pointers are rejected.
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == p1.hex)
                    .values(
                        active_turn_task_id=task2.hex,
                        active_turn_lane="interactive",
                    )
                )

            # Cross-project checkpoint pointers are rejected.
            p2_checkpoint = connection.scalar(
                sa.select(checkpoint.c.id).where(checkpoint.c.project_id == p2.hex)
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == p1.hex)
                    .values(previous_checkpoint_id=p2_checkpoint)
                )

            # Legacy bootstrap may adopt only a v1 checkpoint from the same
            # authority generation; mismatched lineage cannot become current.
            bootstrap_project_id = uuid.uuid4()
            mismatched_checkpoint_id = uuid.uuid4().hex
            connection.execute(
                project.insert().values(
                    id=bootstrap_project_id,
                    vision={},
                    problem_model={},
                    directions=[],
                    strategic_memory_notes=[],
                    running_update=None,
                    paused=False,
                )
            )
            connection.execute(
                resident.insert().values(
                    project_id=bootstrap_project_id.hex,
                    authority_generation=2,
                )
            )
            connection.execute(
                checkpoint.insert().values(
                    id=mismatched_checkpoint_id,
                    project_id=bootstrap_project_id.hex,
                    version=1,
                    schema_version=1,
                    content_hash="9" * 64,
                    checkpoint={"schema_version": 1},
                    authority_generation=1,
                    source_turn_task_id=None,
                )
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == bootstrap_project_id.hex)
                    .values(current_checkpoint_id=mismatched_checkpoint_id)
                )
            connection.execute(
                project.delete().where(project.c.id == bootstrap_project_id)
            )

            # Grant generation 2 to task1 and insert one valid receipt.
            connection.execute(
                resident.update()
                .where(resident.c.project_id == p1.hex)
                .values(
                    engine_owner="strategic_resident",
                    authority_generation=2,
                    active_turn_task_id=task1.hex,
                    active_turn_lane="interactive",
                )
            )
            # Same-generation ordinary state updates remain legal.
            connection.execute(
                resident.update()
                .where(resident.c.project_id == p1.hex)
                .values(health_status="healthy", authority_generation=2)
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == p1.hex)
                    .values(authority_generation=1)
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == p1.hex)
                    .values(engine_owner="legacy", authority_generation=2)
                )
            valid_turn_payload = [
                {
                    "type": "strategic_resident_turn",
                    "resident_id": str(p1),
                    "authority_generation": 2,
                    "engine_owner": "strategic_resident",
                    "checkpoint_version": 1,
                    "checkpoint_hash": checkpoint_row["content_hash"],
                }
            ]
            valid_turn_context = {
                "_resident_mutation_authorized": True,
                "_resident_input_frozen": True,
                "_resident_project_id": str(p1),
                "_resident_authority_generation": 2,
                "_resident_engine_owner_observed": "strategic_resident",
            }
            connection.execute(
                task.update()
                .where(task.c.id == task1)
                .values(payloads=valid_turn_payload, context=valid_turn_context)
            )
            valid_values = {
                "id": uuid.uuid4().hex,
                "project_id": p1.hex,
                "turn_task_id": task1.hex,
                "authority_generation": 2,
                "operation_id": "op-1",
                "operation_kind": "project_mutation",
                "capability": "update_direction",
                "request_hash": "a" * 64,
                "expected_revisions": {
                    "authority_generation": 2,
                    "active_turn_task_version": 1,
                    "current_checkpoint_id": original_checkpoint_id,
                    "resident_checkpoint_version": checkpoint_row["version"],
                    "resident_checkpoint_hash": checkpoint_row["content_hash"],
                },
            }

            # Receipt authority is bound to the immutable Task snapshot, not
            # merely a caller-supplied current generation.
            for operation_id, payloads, context in (
                (
                    "op-stale-payload-generation",
                    [
                        {
                            **valid_turn_payload[0],
                            "authority_generation": 1,
                        }
                    ],
                    valid_turn_context,
                ),
                (
                    "op-stale-context-generation",
                    valid_turn_payload,
                    {
                        **valid_turn_context,
                        "_resident_authority_generation": 1,
                    },
                ),
                (
                    "op-stale-checkpoint-hash",
                    [
                        {
                            **valid_turn_payload[0],
                            "checkpoint_hash": "f" * 64,
                        }
                    ],
                    valid_turn_context,
                ),
                (
                    "op-cross-project-lineage",
                    [
                        {
                            **valid_turn_payload[0],
                            "resident_id": str(p2),
                        }
                    ],
                    valid_turn_context,
                ),
            ):
                connection.execute(
                    task.update()
                    .where(task.c.id == task1)
                    .values(payloads=payloads, context=context)
                )
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(
                        receipt.insert().values(
                            **{
                                **valid_values,
                                "id": uuid.uuid4().hex,
                                "operation_id": operation_id,
                            }
                        )
                    )
            connection.execute(
                task.update()
                .where(task.c.id == task1)
                .values(payloads=valid_turn_payload, context=valid_turn_context)
            )
            for revision_key in (
                "authority_generation",
                "active_turn_task_version",
                "current_checkpoint_id",
                "resident_checkpoint_version",
                "resident_checkpoint_hash",
            ):
                missing = dict(valid_values["expected_revisions"])
                missing.pop(revision_key)
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(
                        receipt.insert().values(
                            **{
                                **valid_values,
                                "id": uuid.uuid4().hex,
                                "operation_id": f"op-missing-{revision_key}",
                                "expected_revisions": missing,
                            }
                        )
                    )
                stale = dict(valid_values["expected_revisions"])
                stale[revision_key] = (
                    "f" * 64
                    if revision_key == "resident_checkpoint_hash"
                    else (
                        uuid.uuid4().hex
                        if revision_key == "current_checkpoint_id"
                        else 99
                    )
                )
                with pytest.raises(sa.exc.IntegrityError):
                    connection.execute(
                        receipt.insert().values(
                            **{
                                **valid_values,
                                "id": uuid.uuid4().hex,
                                "operation_id": f"op-stale-{revision_key}",
                                "expected_revisions": stale,
                            }
                        )
                    )
            connection.execute(receipt.insert().values(**valid_values))
            connection.execute(
                receipt.update()
                .where(receipt.c.operation_id == "op-1")
                .values(status="applying")
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    receipt.update()
                    .where(receipt.c.operation_id == "op-1")
                    .values(operation_kind="different")
                )
            connection.execute(
                receipt.update()
                .where(receipt.c.operation_id == "op-1")
                .values(status="applied", completed_at=sa.func.current_timestamp())
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    receipt.update()
                    .where(receipt.c.operation_id == "op-1")
                    .values(error_code="late-change")
                )

            provenance_checkpoint_id = uuid.uuid4().hex
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    checkpoint.insert().values(
                        id=uuid.uuid4().hex,
                        project_id=p1.hex,
                        version=2,
                        schema_version=1,
                        content_hash="e" * 64,
                        checkpoint={"schema_version": 1},
                        authority_generation=2,
                        source_turn_task_id=None,
                    )
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    checkpoint.insert().values(
                        id=uuid.uuid4().hex,
                        project_id=p1.hex,
                        version=2,
                        schema_version=1,
                        content_hash="e" * 64,
                        checkpoint={"schema_version": 1},
                        authority_generation=2,
                        source_turn_task_id=task2.hex,
                    )
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    checkpoint.insert().values(
                        id=uuid.uuid4().hex,
                        project_id=p1.hex,
                        version=3,
                        schema_version=1,
                        content_hash="e" * 64,
                        checkpoint={"schema_version": 1},
                        authority_generation=2,
                        source_turn_task_id=task1.hex,
                    )
                )
            connection.execute(
                task.update()
                .where(task.c.id == task1)
                .values(
                    payloads=[
                        {
                            **valid_turn_payload[0],
                            "checkpoint_version": 0,
                        }
                    ]
                )
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    checkpoint.insert().values(
                        id=uuid.uuid4().hex,
                        project_id=p1.hex,
                        version=2,
                        schema_version=1,
                        content_hash="c" * 64,
                        checkpoint={"schema_version": 1},
                        authority_generation=2,
                        source_turn_task_id=task1.hex,
                    )
                )
            connection.execute(
                task.update()
                .where(task.c.id == task1)
                .values(payloads=valid_turn_payload, context=valid_turn_context)
            )
            connection.execute(
                checkpoint.insert().values(
                    id=provenance_checkpoint_id,
                    project_id=p1.hex,
                    version=2,
                    schema_version=1,
                    content_hash="d" * 64,
                    checkpoint={"schema_version": 1},
                    authority_generation=2,
                    source_turn_task_id=task1.hex,
                )
            )
            connection.execute(
                resident.update()
                .where(resident.c.project_id == p1.hex)
                .values(
                    previous_checkpoint_id=original_checkpoint_id,
                    current_checkpoint_id=provenance_checkpoint_id,
                )
            )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == p1.hex)
                    .values(previous_checkpoint_id=None)
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == p1.hex)
                    .values(current_checkpoint_id=None)
                )

            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    receipt.insert().values(
                        **{
                            **valid_values,
                            "id": uuid.uuid4().hex,
                            "operation_id": "op-stale",
                            "authority_generation": 1,
                        }
                    )
                )
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    receipt.insert().values(
                        **{
                            **valid_values,
                            "id": uuid.uuid4().hex,
                            "operation_id": "op-foreign",
                            "turn_task_id": task2.hex,
                        }
                    )
                )

            # Task project moves cannot strand active/successor pointers.
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    task.update().where(task.c.id == task1).values(project_id=p2)
                )

            # Task cleanup clears authority pointers but preserves receipt UUID.
            connection.execute(task.delete().where(task.c.id == task1))
            assert (
                connection.scalar(
                    sa.select(resident.c.active_turn_task_id).where(
                        resident.c.project_id == p1.hex
                    )
                )
                is None
            )
            assert (
                connection.scalar(
                    sa.select(resident.c.active_turn_lane).where(
                        resident.c.project_id == p1.hex
                    )
                )
                is None
            )
            assert (
                connection.scalar(
                    sa.select(receipt.c.turn_task_id).where(
                        receipt.c.operation_id == "op-1"
                    )
                )
                == task1.hex
            )
            assert (
                connection.scalar(
                    sa.select(checkpoint.c.source_turn_task_id).where(
                        checkpoint.c.id == provenance_checkpoint_id
                    )
                )
                == task1.hex
            )

            # Backfill work scales with bounded chunks, not one statement set
            # per project. Thirty new rows require two 25-row chunks.
            new_projects = [
                {
                    "id": uuid.uuid4(),
                    "vision": {},
                    "problem_model": {},
                    "directions": [],
                    "strategic_memory_notes": [],
                    "running_update": None,
                    "paused": False,
                }
                for _ in range(30)
            ]
            connection.execute(project.insert(), new_projects)
            statements: list[str] = []

            def _capture(_conn, _cursor, statement, _params, _ctx, _many):
                statements.append(statement)

            sa.event.listen(engine, "before_cursor_execute", _capture)
            try:
                migration._backfill(postgresql_dialect=False)
            finally:
                sa.event.remove(engine, "before_cursor_execute", _capture)
            assert len(statements) <= 12
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(resident))
                == 32
            )
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(checkpoint))
                == 33
            )  # 32 v1 rows plus the provenance v2 row

            # A project-owned Resident and all checkpoints cascade cleanly even
            # though the current pointer is protected against ordinary clearing.
            connection.execute(project.delete().where(project.c.id == p2))
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(resident)
                    .where(resident.c.project_id == p2.hex)
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(checkpoint)
                    .where(checkpoint.c.project_id == p2.hex)
                )
                == 0
            )

            migration.downgrade()
            inspector = sa.inspect(connection)
            assert not inspector.has_table("strategicoperationreceipt")
            assert not inspector.has_table("strategicresidentcheckpoint")
            assert not inspector.has_table("strategicresident")
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM sqlite_master "
                        "WHERE type='trigger' AND name LIKE '%strategicresident%'"
                    )
                )
                == 0
            )
        finally:
            migration.op = original_op


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_upgrade_installs_and_exercises_authority_triggers():
    """Compile and exercise the PostgreSQL-only PL/pgSQL trigger path."""

    migration = _load_migration()
    async with ephemeral_postgres_database(make_url(os.environ["DATABASE_URL"])) as (
        engine,
        _name,
    ):

        def _exercise(connection):
            project, task, _prefs, _message = _sqlite_legacy_schema(connection)
            project_id = uuid.uuid4()
            turn_task_id = uuid.uuid4()
            connection.execute(
                project.insert().values(
                    id=project_id,
                    vision={},
                    problem_model={},
                    directions=[],
                    strategic_memory_notes=[],
                    running_update=None,
                    paused=False,
                )
            )
            connection.execute(
                task.insert().values(
                    id=turn_task_id,
                    project_id=project_id,
                    status="IN_PROGRESS",
                    payloads=[{"type": "strategic_resident_turn"}],
                    context={"_resident_mutation_authorized": True},
                )
            )

            original_op = migration.op
            migration.op = Operations(MigrationContext.configure(connection))
            try:
                migration.upgrade()
                resident = sa.Table(
                    "strategicresident",
                    sa.MetaData(),
                    autoload_with=connection,
                )
                checkpoint = sa.Table(
                    "strategicresidentcheckpoint",
                    sa.MetaData(),
                    autoload_with=connection,
                )
                receipt = sa.Table(
                    "strategicoperationreceipt",
                    sa.MetaData(),
                    autoload_with=connection,
                )
                initial_checkpoint_id = connection.scalar(
                    sa.select(resident.c.current_checkpoint_id).where(
                        resident.c.project_id == project_id
                    )
                )
                initial_checkpoint = (
                    connection.execute(
                        sa.select(checkpoint).where(
                            checkpoint.c.id == initial_checkpoint_id
                        )
                    )
                    .mappings()
                    .one()
                )
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == project_id)
                    .values(
                        engine_owner="strategic_resident",
                        authority_generation=2,
                        active_turn_task_id=turn_task_id,
                        active_turn_lane="interactive",
                    )
                )
                connection.execute(
                    task.update()
                    .where(task.c.id == turn_task_id)
                    .values(
                        payloads=[
                            {
                                "type": "strategic_resident_turn",
                                "resident_id": str(project_id),
                                "authority_generation": 2,
                                "engine_owner": "strategic_resident",
                                "checkpoint_version": initial_checkpoint["version"],
                                "checkpoint_hash": initial_checkpoint["content_hash"],
                            }
                        ],
                        context={
                            "_resident_mutation_authorized": True,
                            "_resident_input_frozen": True,
                            "_resident_project_id": str(project_id),
                            "_resident_authority_generation": 2,
                            "_resident_engine_owner_observed": "strategic_resident",
                        },
                    )
                )
                expected_revisions = {
                    "authority_generation": 2,
                    "active_turn_task_version": 1,
                    "current_checkpoint_id": str(initial_checkpoint_id),
                    "resident_checkpoint_version": initial_checkpoint["version"],
                    "resident_checkpoint_hash": initial_checkpoint["content_hash"],
                }
                connection.execute(
                    receipt.insert().values(
                        id=uuid.uuid4(),
                        project_id=project_id,
                        turn_task_id=turn_task_id,
                        authority_generation=2,
                        operation_id="pg-valid",
                        operation_kind="project_mutation",
                        capability="update_direction",
                        request_hash="a" * 64,
                        expected_revisions=expected_revisions,
                    )
                )
                with (
                    pytest.raises(sa.exc.IntegrityError),
                    connection.begin_nested(),
                ):
                    connection.execute(
                        receipt.insert().values(
                            id=uuid.uuid4(),
                            project_id=project_id,
                            turn_task_id=turn_task_id,
                            authority_generation=1,
                            operation_id="pg-stale",
                            operation_kind="project_mutation",
                            capability="update_direction",
                            request_hash="b" * 64,
                            expected_revisions=expected_revisions,
                        )
                    )

                next_checkpoint_id = uuid.uuid4()
                connection.execute(
                    checkpoint.insert().values(
                        id=next_checkpoint_id,
                        project_id=project_id,
                        version=2,
                        schema_version=1,
                        content_hash="c" * 64,
                        checkpoint={"schema_version": 1},
                        authority_generation=2,
                        source_turn_task_id=turn_task_id,
                    )
                )
                connection.execute(
                    resident.update()
                    .where(resident.c.project_id == project_id)
                    .values(
                        previous_checkpoint_id=initial_checkpoint_id,
                        current_checkpoint_id=next_checkpoint_id,
                    )
                )
                with (
                    pytest.raises(sa.exc.IntegrityError),
                    connection.begin_nested(),
                ):
                    connection.execute(
                        resident.update()
                        .where(resident.c.project_id == project_id)
                        .values(current_checkpoint_id=None)
                    )
                migration.downgrade()
            finally:
                migration.op = original_op

        async with engine.begin() as connection:
            await connection.run_sync(_exercise)
