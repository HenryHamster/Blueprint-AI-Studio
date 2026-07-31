"""Read-only preflight report for Alembic 0711 correctness repairs.

Run before deployment:

    python -m app.strategic_correctness_preflight --database-url "$DATABASE_URL"

The command starts a read-only transaction on PostgreSQL, emits counts as JSON,
and never performs a write.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import uuid
from collections.abc import Mapping

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

_BATCH_SIZE = 500
_PLACEHOLDER_RE = re.compile(r"^<[^>]+>$")


def _object(value: object) -> dict | None:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
        return dict(value) if isinstance(value, Mapping) else None
    return None


def _normalized(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _is_uuid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value.lower()


def _uuid_key(value: object) -> str:
    """Normalize UUIDs from both typed and reflected SQLite result rows."""
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return str(value)


def _reply_reference_ids(
    reply_to_message_id: object, context: object
) -> set[uuid.UUID]:
    references: set[uuid.UUID] = set()
    if isinstance(reply_to_message_id, uuid.UUID):
        references.add(reply_to_message_id)
    elif _is_uuid_text(reply_to_message_id):
        references.add(uuid.UUID(str(reply_to_message_id)))
    payload = _object(context) or {}
    covered = payload.get("_resident_reply_covered_message_ids")
    if isinstance(covered, list):
        references.update(
            uuid.UUID(value) for value in covered if _is_uuid_text(value)
        )
    return references


def _purpose_reference(value: object) -> tuple[uuid.UUID, str] | None:
    model = _object(value)
    intent = _object(model.get("intent")) if model else None
    if intent is None:
        return None
    user_purpose = (
        intent.get("user_purpose").strip()
        if isinstance(intent.get("user_purpose"), str)
        else ""
    )
    if user_purpose and user_purpose != "..." and not _PLACEHOLDER_RE.fullmatch(
        user_purpose
    ):
        return None
    purpose = intent.get("purpose")
    normalized_purpose = purpose.strip() if isinstance(purpose, str) else ""
    if (
        not normalized_purpose
        or normalized_purpose == "..."
        or _PLACEHOLDER_RE.fullmatch(normalized_purpose)
    ):
        return None
    source = _object(intent.get("source"))
    if source is None or source.get("type") != "user_message":
        return None
    try:
        return uuid.UUID(str(source.get("message_id"))), normalized_purpose
    except (TypeError, ValueError, AttributeError):
        return None


def _is_purpose_candidate(value: object) -> bool:
    model = _object(value)
    intent = _object(model.get("intent")) if model else None
    if intent is None:
        return False
    user_purpose = (
        intent.get("user_purpose").strip()
        if isinstance(intent.get("user_purpose"), str)
        else ""
    )
    if user_purpose and user_purpose != "..." and not _PLACEHOLDER_RE.fullmatch(
        user_purpose
    ):
        return False
    purpose = (
        intent.get("purpose").strip()
        if isinstance(intent.get("purpose"), str)
        else ""
    )
    source = _object(intent.get("source"))
    return bool(
        purpose
        and purpose != "..."
        and not _PLACEHOLDER_RE.fullmatch(purpose)
        and source
        and source.get("type") == "user_message"
    )


def analyze_connection(
    connection: sa.Connection, *, batch_size: int = _BATCH_SIZE
) -> dict[str, int]:
    metadata = sa.MetaData()
    project = sa.Table("project", metadata, autoload_with=connection)
    message = sa.Table("projectmessage", metadata, autoload_with=connection)
    report = {
        "purpose_candidates": 0,
        "purpose_repairs": 0,
        "purpose_ambiguous_or_drifted": 0,
        "reply_candidates": 0,
        "reply_repairs": 0,
        "reply_ambiguous": 0,
    }

    projects = connection.execute(
        sa.select(project.c.id, project.c.problem_model).where(
            project.c.problem_model.is_not(None)
        )
    )
    while batch := projects.fetchmany(batch_size):
        refs: list[tuple[object, uuid.UUID, str]] = []
        for row in batch:
            if not _is_purpose_candidate(row.problem_model):
                continue
            report["purpose_candidates"] += 1
            reference = _purpose_reference(row.problem_model)
            if reference is None:
                report["purpose_ambiguous_or_drifted"] += 1
                continue
            refs.append((row.id, *reference))
        ids = [message_id for _project_id, message_id, _purpose in refs]
        evidence = {
            (_uuid_key(row.project_id), _uuid_key(row.id)): row.content
            for row in connection.execute(
                sa.select(
                    message.c.id, message.c.project_id, message.c.content
                ).where(
                    message.c.id.in_(ids),
                    message.c.from_type == "user",
                )
            )
        }
        for project_id, message_id, purpose in refs:
            content = evidence.get((_uuid_key(project_id), _uuid_key(message_id)))
            if isinstance(content, str) and _normalized(content) == _normalized(
                purpose
            ):
                report["purpose_repairs"] += 1
            else:
                report["purpose_ambiguous_or_drifted"] += 1

    replies = connection.execute(
        sa.select(
            message.c.project_id,
            message.c.reply_to_message_id,
            message.c.context,
        ).where(
            message.c.surface == "conversation",
            message.c.from_type == "strategic",
            message.c.handling_state == "responded",
            message.c.status == "open",
        )
    )
    while batch := replies.fetchmany(batch_size):
        report["reply_candidates"] += len(batch)
        references_by_row = [
            _reply_reference_ids(row.reply_to_message_id, row.context)
            for row in batch
        ]
        reference_ids = {
            reference_id
            for references in references_by_row
            for reference_id in references
        }
        verified_inputs = {
            (_uuid_key(row.project_id), _uuid_key(row.id))
            for row in connection.execute(
                sa.select(message.c.id, message.c.project_id).where(
                    message.c.id.in_(reference_ids),
                    message.c.surface == "conversation",
                    message.c.from_type.in_(("user", "operator")),
                )
            )
        }
        for row, references in zip(batch, references_by_row, strict=True):
            proven = any(
                (_uuid_key(row.project_id), _uuid_key(reference_id)) in verified_inputs
                for reference_id in references
            )
            report["reply_repairs" if proven else "reply_ambiguous"] += 1
    return report


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
    if database_url.startswith("postgres://"):
        return database_url.replace(
            "postgres://", "postgresql+asyncpg://", 1
        )
    if database_url.startswith("sqlite://") and not database_url.startswith(
        "sqlite+aiosqlite://"
    ):
        return database_url.replace(
            "sqlite://", "sqlite+aiosqlite://", 1
        )
    return database_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="SQLAlchemy database URL (defaults to DATABASE_URL)",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    database_url = _async_database_url(str(args.database_url))

    async def _run() -> dict[str, int]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    if connection.dialect.name == "postgresql":
                        await connection.execute(
                            sa.text("SET TRANSACTION READ ONLY")
                        )
                    return await connection.run_sync(analyze_connection)
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    report = asyncio.run(_run())
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
