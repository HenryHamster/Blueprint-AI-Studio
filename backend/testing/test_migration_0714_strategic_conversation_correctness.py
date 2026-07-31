"""Focused executable coverage for migration 0714."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = Path(__file__).resolve().parents[1] / (
        "alembic/versions/0714_repair_strategic_conversation_correctness.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0711", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(connection):
    metadata = sa.MetaData()
    project = sa.Table(
        "project",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("problem_model", sa.JSON()),
    )
    message = sa.Table(
        "projectmessage",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("from_type", sa.String(), nullable=False),
        sa.Column("handling_state", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reply_to_message_id", sa.Uuid()),
        sa.Column("content", sa.Text()),
        sa.Column("context", sa.JSON()),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )
    metadata.create_all(connection)
    return project, message


def _run_upgrade(migration, connection) -> None:
    original_op = migration.op
    migration.op = Operations(MigrationContext.configure(connection))
    try:
        migration.upgrade()
    finally:
        migration.op = original_op


def _model(
    purpose: str,
    user_purpose: str,
    *,
    message_id: uuid.UUID | str | None,
    source_type: str = "user_message",
) -> dict:
    return {
        "intent": {
            "purpose": purpose,
            "user_purpose": user_purpose,
            "source": {
                "type": source_type,
                "message_id": str(message_id) if message_id is not None else None,
            },
        }
    }


def test_upgrade_repairs_only_proven_user_purpose_and_operator_replies():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        project, message = _schema(connection)
        trusted_project = uuid.uuid4()
        foreign_project = uuid.uuid4()
        drift_project = uuid.uuid4()
        trusted_message = uuid.uuid4()
        foreign_message = uuid.uuid4()
        drift_message = uuid.uuid4()
        reply_target = uuid.uuid4()
        reply_by_link = uuid.uuid4()
        reply_by_role = uuid.uuid4()
        reply_by_coverage = uuid.uuid4()
        ambiguous_reply = uuid.uuid4()
        wrong_surface_reply = uuid.uuid4()
        cross_project_reply = uuid.uuid4()
        internal_reference_reply = uuid.uuid4()
        internal_message = uuid.uuid4()

        for message_row in [
                {
                    "id": trusted_message,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "user",
                    "handling_state": "pending",
                    "status": "open",
                    "content": "Build the reliable thing",
                    "context": {},
                },
                {
                    "id": foreign_message,
                    "project_id": foreign_project,
                    "surface": "conversation",
                    "from_type": "user",
                    "handling_state": "pending",
                    "status": "open",
                    "content": "Foreign source content",
                    "context": {},
                },
                {
                    "id": drift_message,
                    "project_id": drift_project,
                    "surface": "conversation",
                    "from_type": "user",
                    "handling_state": "pending",
                    "status": "open",
                    "content": "Original purpose before drift",
                    "context": {},
                },
                {
                    "id": reply_target,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "user",
                    "handling_state": "responded",
                    "status": "handled",
                    "content": "",
                    "context": {},
                },
                {
                    "id": internal_message,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "strategic",
                    "handling_state": "responded",
                    "status": "resolved",
                    "content": "Internal output",
                    "context": {},
                },
                {
                    "id": reply_by_link,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "strategic",
                    "handling_state": "responded",
                    "status": "open",
                    "reply_to_message_id": reply_target,
                    "content": "",
                    "context": {},
                },
                {
                    "id": reply_by_role,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "strategic",
                    "handling_state": "responded",
                    "status": "open",
                    "content": "",
                    "context": {"message_role": "operator_reply"},
                },
                {
                    "id": reply_by_coverage,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "strategic",
                    "handling_state": "responded",
                    "status": "open",
                    "content": "",
                    "context": {
                        "_resident_reply_covered_message_ids": [str(trusted_message)]
                    },
                },
                {
                    "id": ambiguous_reply,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "strategic",
                    "handling_state": "responded",
                    "status": "open",
                    "content": "",
                    "context": {},
                },
                {
                    "id": wrong_surface_reply,
                    "project_id": trusted_project,
                    "surface": "activity",
                    "from_type": "strategic",
                    "handling_state": "responded",
                    "status": "open",
                    "content": "",
                    "context": {"message_role": "operator_reply"},
                },
                {
                    "id": cross_project_reply,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "strategic",
                    "handling_state": "responded",
                    "status": "open",
                    "reply_to_message_id": foreign_message,
                    "content": "",
                    "context": {},
                },
                {
                    "id": internal_reference_reply,
                    "project_id": trusted_project,
                    "surface": "conversation",
                    "from_type": "strategic",
                    "handling_state": "responded",
                    "status": "open",
                    "content": "Not operator evidence",
                    "context": {
                        "_resident_reply_covered_message_ids": [
                            str(internal_message)
                        ]
                    },
                },
        ]:
            # Execute separately so optional reply_to_message_id/content keys
            # are not lost to executemany's first-row column shape.
            connection.execute(message.insert(), message_row)

        project_ids = {
            "trusted": trusted_project,
            "foreign_source": uuid.uuid4(),
            "agent_source": uuid.uuid4(),
            "existing": uuid.uuid4(),
            "placeholder": uuid.uuid4(),
            "drift": drift_project,
        }
        connection.execute(
            project.insert(),
            [
                {
                    "id": project_ids["trusted"],
                    "problem_model": _model(
                        "Build the reliable thing",
                        "",
                        message_id=trusted_message,
                    ),
                },
                {
                    "id": project_ids["foreign_source"],
                    "problem_model": _model(
                        "Do not trust this",
                        "",
                        message_id=foreign_message,
                    ),
                },
                {
                    "id": project_ids["agent_source"],
                    "problem_model": _model(
                        "Agent-authored",
                        "",
                        message_id=trusted_message,
                        source_type="agent_observation",
                    ),
                },
                {
                    "id": project_ids["existing"],
                    "problem_model": _model(
                        "New inferred purpose",
                        "Original operator purpose",
                        message_id=trusted_message,
                    ),
                },
                {
                    "id": project_ids["placeholder"],
                    "problem_model": _model(
                        "<from analysis>",
                        "",
                        message_id=trusted_message,
                    ),
                },
                {
                    "id": project_ids["drift"],
                    "problem_model": _model(
                        "Purpose drifted after the cited message",
                        "",
                        message_id=drift_message,
                    ),
                },
            ],
        )

        _run_upgrade(migration, connection)
        rerun_counts = migration._portable_repair(connection)
        assert rerun_counts[0] == 0
        assert rerun_counts[2] == 0

        models = {
            row.id: row.problem_model
            for row in connection.execute(
                sa.select(project.c.id, project.c.problem_model)
            )
        }
        assert (
            models[project_ids["trusted"]]["intent"]["user_purpose"]
            == "Build the reliable thing"
        )
        assert (
            models[project_ids["foreign_source"]]["intent"]["user_purpose"] == ""
        )
        assert models[project_ids["agent_source"]]["intent"]["user_purpose"] == ""
        assert (
            models[project_ids["existing"]]["intent"]["user_purpose"]
            == "Original operator purpose"
        )
        assert models[project_ids["placeholder"]]["intent"]["user_purpose"] == ""
        assert models[project_ids["drift"]]["intent"]["user_purpose"] == ""

        statuses = dict(
            connection.execute(
                sa.select(message.c.id, message.c.status).where(
                    message.c.id.in_(
                        [
                            reply_by_link,
                            reply_by_role,
                            reply_by_coverage,
                            ambiguous_reply,
                            wrong_surface_reply,
                            cross_project_reply,
                            internal_reference_reply,
                        ]
                    )
                )
            ).all()
        )
        assert statuses[reply_by_link] == "resolved"
        assert statuses[reply_by_role] == "open"
        assert statuses[reply_by_coverage] == "resolved"
        assert statuses[ambiguous_reply] == "open"
        assert statuses[wrong_surface_reply] == "open"
        assert statuses[cross_project_reply] == "open"
        assert statuses[internal_reference_reply] == "open"


def test_provenance_helpers_reject_malformed_and_empty_markers():
    migration = _migration()
    project_id = uuid.uuid4()
    message_id = uuid.uuid4()
    users = {(str(project_id), str(message_id)): "Real purpose"}

    assert (
        migration._trusted_user_purpose(
            _model("Real purpose", "...", message_id=message_id),
            project_id=project_id,
            user_messages=users,
        )
        == "Real purpose"
    )
    assert (
        migration._trusted_user_purpose(
            _model("Real purpose", "", message_id="not-a-uuid"),
            project_id=project_id,
            user_messages=users,
        )
        is None
    )
    assert migration._reply_reference_ids(
        None, {"_resident_reply_covered_message_ids": []}
    ) == set()
    assert migration._reply_reference_ids(
        None, {"_resident_reply_covered_message_ids": ["not-a-uuid"]}
    ) == set()
    assert migration._reply_reference_ids(
        None, {"message_role": "internal_update"}
    ) == set()


def test_postgres_compatibility_contract_and_trigram_index_are_declared():
    source = Path(migration_path := _migration().__file__).read_text()
    assert migration_path.endswith(
        "0714_repair_strategic_conversation_correctness.py"
    )
    assert "trg_0711_repair_reply_status" in source
    assert "trg_0711_fill_user_purpose_from_message" in source
    assert "trg_0711_fill_user_purpose_from_project" in source
    assert "regexp_replace(btrim(NEW.content)" in source
    assert "ix_projectmessage_content_trgm" in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in source
    assert "NOT i.indisvalid" in source
    assert source.index("NOT i.indisvalid") < source.index(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS"
    )
    assert "covered.value ~ '{_PG_UUID_RE}'" in source
    assert "covered.value ~* " not in source
    assert "DROP TRIGGER IF EXISTS" in source
    assert source.index(
        "DROP TRIGGER IF EXISTS trg_0711_repair_reply_status"
    ) < source.index("CREATE TRIGGER trg_0711_repair_reply_status")
    assert source.index(
        "DROP TRIGGER IF EXISTS trg_0711_fill_user_purpose_from_message"
    ) < source.index(
        "CREATE TRIGGER trg_0711_fill_user_purpose_from_message"
    )
    assert source.index(
        "DROP TRIGGER IF EXISTS trg_0711_fill_user_purpose_from_project"
    ) < source.index(
        "CREATE TRIGGER trg_0711_fill_user_purpose_from_project"
    )
