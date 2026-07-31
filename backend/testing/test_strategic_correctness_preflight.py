from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.strategic_correctness_preflight import (
    _async_database_url,
    analyze_connection,
)


def test_cli_database_url_normalization_supports_app_async_driver():
    async_url = "postgresql+asyncpg://user:secret@db.example/blueprint"
    assert _async_database_url(async_url) == async_url
    assert _async_database_url(
        "postgresql://user:secret@db.example/blueprint"
    ) == async_url
    assert _async_database_url(
        "postgres://user:secret@db.example/blueprint"
    ) == async_url


def test_preflight_is_read_only_and_distinguishes_drift():
    engine = sa.create_engine("sqlite://")
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
        sa.Column("project_id", sa.Uuid()),
        sa.Column("from_type", sa.String()),
        sa.Column("surface", sa.String()),
        sa.Column("handling_state", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("reply_to_message_id", sa.Uuid()),
        sa.Column("content", sa.Text()),
        sa.Column("context", sa.JSON()),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        matching_project, drift_project = uuid.uuid4(), uuid.uuid4()
        matching_message, drift_message = uuid.uuid4(), uuid.uuid4()
        connection.execute(
            message.insert(),
            [
                {
                    "id": matching_message,
                    "project_id": matching_project,
                    "from_type": "user",
                    "surface": "conversation",
                    "handling_state": "pending",
                    "status": "open",
                    "content": "Ship   reliable memory",
                    "context": {},
                },
                {
                    "id": drift_message,
                    "project_id": drift_project,
                    "from_type": "user",
                    "surface": "conversation",
                    "handling_state": "pending",
                    "status": "open",
                    "content": "Original purpose",
                    "context": {},
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": matching_project,
                    "from_type": "strategic",
                    "surface": "conversation",
                    "handling_state": "responded",
                    "status": "open",
                    "content": "Reply",
                    "context": {
                        "_resident_reply_covered_message_ids": [
                            str(matching_message)
                        ]
                    },
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": matching_project,
                    "from_type": "strategic",
                    "surface": "conversation",
                    "handling_state": "responded",
                    "status": "open",
                    "content": "Ambiguous",
                    "context": {"message_role": "operator_reply"},
                },
                {
                    "id": uuid.uuid4(),
                    "project_id": matching_project,
                    "from_type": "strategic",
                    "surface": "conversation",
                    "handling_state": "responded",
                    "status": "open",
                    "reply_to_message_id": drift_message,
                    "content": "Cross-project reference",
                    "context": {},
                },
            ],
        )

        def model(purpose: str, message_id: uuid.UUID) -> dict:
            return {
                "intent": {
                    "purpose": purpose,
                    "user_purpose": "",
                    "source": {
                        "type": "user_message",
                        "message_id": str(message_id),
                    },
                }
            }

        connection.execute(
            project.insert(),
            [
                {
                    "id": matching_project,
                    "problem_model": model(
                        "Ship reliable memory", matching_message
                    ),
                },
                {
                    "id": drift_project,
                    "problem_model": model("Drifted purpose", drift_message),
                },
            ],
        )
        before = connection.execute(sa.select(project)).all()
        report = analyze_connection(connection, batch_size=1)
        after = connection.execute(sa.select(project)).all()

    assert report == {
        "purpose_candidates": 2,
        "purpose_repairs": 1,
        "purpose_ambiguous_or_drifted": 1,
        "reply_candidates": 3,
        "reply_repairs": 1,
        "reply_ambiguous": 2,
    }
    assert after == before
