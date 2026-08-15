"""Canonical read facade for the durable Strategic operator conversation.

The admin UI, Workday, and future foreground Residents must not grow separate
definitions of "the conversation". This module owns the status-independent
message predicate and sequence cursor behavior. Callers retain responsibility
for authorization and for projecting rows into their audience-specific shape.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Boolean, and_, exists, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import aliased
from sqlalchemy.sql.functions import FunctionElement
from sqlmodel import select

from app.message_audience import PIPELINE_LOG, STRATEGIST_INTERNAL
from app.models import MessageSurface, ProjectMessage

_CHECK_IN_EVENT_TYPE = "strategic_check_in"
_MODE_KNOB_CUT_EVENT_TYPE = "mode_knob_cut_recommended"
_EVENT_TYPE_CONTEXT_KEY = "event_type"
_AUDIENCE_CONTEXT_KEY = "audience"
_COVERED_MESSAGE_IDS_KEY = "_resident_reply_covered_message_ids"
_CONVERSATION_VISIBILITY_CONTEXT_KEYS = frozenset(
    {
        _EVENT_TYPE_CONTEXT_KEY,
        _AUDIENCE_CONTEXT_KEY,
        _COVERED_MESSAGE_IDS_KEY,
    }
)
_CANONICAL_UUID_RE = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    "[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SQLITE_CANONICAL_UUID_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)


class _HasVerifiedCoveredInput(FunctionElement):
    """Dialect-portable proof that a receipt names a project operator input."""

    type = Boolean()
    inherit_cache = True


@compiles(_HasVerifiedCoveredInput, "postgresql")
def _compile_verified_covered_input_postgresql(element, compiler, **kw) -> str:
    context_sql = compiler.process(next(iter(element.clauses)), **kw)
    project_id_sql = compiler.process(ProjectMessage.project_id, **kw)
    return (
        "EXISTS (SELECT 1 FROM jsonb_array_elements_text("
        f"CASE WHEN jsonb_typeof({context_sql} -> '{_COVERED_MESSAGE_IDS_KEY}') "
        f"= 'array' THEN {context_sql} -> '{_COVERED_MESSAGE_IDS_KEY}' "
        "ELSE '[]'::jsonb END"
        ") AS covered(value) "
        "JOIN projectmessage AS covered_input "
        "ON covered_input.id::text = covered.value "
        f"WHERE covered.value ~ '{_CANONICAL_UUID_RE}' "
        f"AND covered_input.project_id = {project_id_sql} "
        "AND covered_input.surface = 'conversation' "
        "AND covered_input.from_type IN ('user', 'operator'))"
    )


@compiles(_HasVerifiedCoveredInput, "sqlite")
def _compile_verified_covered_input_sqlite(element, compiler, **kw) -> str:
    context_sql = compiler.process(next(iter(element.clauses)), **kw)
    project_id_sql = compiler.process(ProjectMessage.project_id, **kw)
    json_path = f'$."{_COVERED_MESSAGE_IDS_KEY}"'
    return (
        "EXISTS (SELECT 1 FROM json_each("
        f"CASE WHEN json_type({context_sql}, '{json_path}') = 'array' "
        f"THEN json_extract({context_sql}, '{json_path}') ELSE '[]' END"
        ") AS covered "
        "JOIN projectmessage AS covered_input "
        "ON lower(replace(CAST(covered_input.id AS TEXT), '-', '')) "
        "= replace(covered.value, '-', '') "
        "WHERE covered.type = 'text' "
        f"AND covered.value GLOB '{_SQLITE_CANONICAL_UUID_GLOB}' "
        f"AND covered_input.project_id = {project_id_sql} "
        "AND covered_input.surface = 'conversation' "
        "AND covered_input.from_type IN ('user', 'operator'))"
    )


@dataclass(frozen=True)
class StrategicConversationPage:
    """One chronological page plus sequence cursors for adjacent pages."""

    rows: tuple[ProjectMessage, ...]
    limit: int
    has_more: bool
    next_before_sequence: int | None
    next_after_sequence: int | None
    target_found: bool | None = None
    order: str = "oldest_first"


def strategic_conversation_predicate() -> Any:
    """Return the canonical operator-conversation visibility predicate.

    Status is intentionally absent: answered/handled rows are conversation
    evidence just as open inputs are. A Strategic sender alone is insufficient
    because internal bookkeeping is also strategist-authored; explicit reply
    provenance admits legacy replies whose audience stamp was internal.
    """

    reply_source = aliased(ProjectMessage)
    reply_to_input = exists(
        select(reply_source.id).where(
            reply_source.id == ProjectMessage.reply_to_message_id,
            reply_source.project_id == ProjectMessage.project_id,
            reply_source.surface == MessageSurface.CONVERSATION,
            reply_source.from_type.in_(("user", "operator")),
        )
    )
    proven_operator_reply = and_(
        ProjectMessage.from_type == "strategic",
        or_(
            reply_to_input,
            _HasVerifiedCoveredInput(ProjectMessage.context),
        ),
    )
    event_type = ProjectMessage.context[_EVENT_TYPE_CONTEXT_KEY].as_string()
    return and_(
        ProjectMessage.surface == MessageSurface.CONVERSATION,
        or_(
            event_type.is_(None),
            event_type != _CHECK_IN_EVENT_TYPE,
        ),
        # CUT recommendations are completion-debt bookkeeping, not human
        # conversation. Keep legacy rows out even though they were persisted on
        # the conversation surface before that routing distinction was fixed.
        or_(
            event_type.is_(None),
            event_type != _MODE_KNOB_CUT_EVENT_TYPE,
        ),
        or_(
            proven_operator_reply,
            ProjectMessage.context[_AUDIENCE_CONTEXT_KEY].as_string().is_(None),
            ~ProjectMessage.context[_AUDIENCE_CONTEXT_KEY]
            .as_string()
            .in_([STRATEGIST_INTERNAL, PIPELINE_LOG]),
        ),
    )


def _search_condition(search: str | None) -> Any | None:
    normalized = (search or "").strip()
    if not normalized:
        return None
    escaped = (
        normalized.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return ProjectMessage.content.ilike(f"%{escaped}%", escape="\\")


async def query_strategic_conversation(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    include_message_id: uuid.UUID | None = None,
    before_sequence: int | None = None,
    after_sequence: int | None = None,
    limit: int = 20,
    search: str | None = None,
) -> StrategicConversationPage:
    """Read one status-independent Strategic conversation page."""

    normalized_search = (search or "").strip() or None
    if (
        include_message_id is not None
        and (
            before_sequence is not None
            or after_sequence is not None
            or normalized_search is not None
        )
    ) or (
        after_sequence is not None
        and (before_sequence is not None or normalized_search is not None)
    ):
        raise ValueError(
            "target and after selectors are exclusive; search may page with before"
        )
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")

    base_conditions: list[Any] = [
        ProjectMessage.project_id == project_id,
        strategic_conversation_predicate(),
    ]
    search_condition = _search_condition(normalized_search)
    if search_condition is not None:
        base_conditions.append(search_condition)

    included_message: ProjectMessage | None = None
    centered_before_cursor: int | None = None
    centered_after_cursor: int | None = None
    if include_message_id is not None:
        included_message = (
            await session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.id == include_message_id,
                    *base_conditions,
                )
            )
        ).scalars().first()
        if included_message is None:
            return StrategicConversationPage(
                rows=(),
                limit=limit,
                has_more=False,
                next_before_sequence=None,
                next_after_sequence=None,
                target_found=False,
            )
        if included_message.sequence is None:
            return StrategicConversationPage(
                rows=(included_message,),
                limit=limit,
                has_more=False,
                next_before_sequence=None,
                next_after_sequence=None,
                target_found=True,
            )

    if included_message is not None and included_message.sequence is not None:
        before_count = limit // 2
        after_count = max(0, limit - before_count - 1)
        before_rows = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    *base_conditions,
                    ProjectMessage.sequence < included_message.sequence,
                )
                .order_by(ProjectMessage.sequence.desc())
                .limit(before_count)
            )
        ).scalars().all()
        after_rows = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    *base_conditions,
                    ProjectMessage.sequence > included_message.sequence,
                )
                .order_by(ProjectMessage.sequence.asc())
                .limit(after_count)
            )
        ).scalars().all()
        missing = limit - (len(before_rows) + 1 + len(after_rows))
        if missing and len(before_rows) < before_count:
            after_rows = (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        *base_conditions,
                        ProjectMessage.sequence > included_message.sequence,
                    )
                    .order_by(ProjectMessage.sequence.asc())
                    .limit(after_count + missing)
                )
            ).scalars().all()
        elif missing and len(after_rows) < after_count:
            before_rows = (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        *base_conditions,
                        ProjectMessage.sequence < included_message.sequence,
                    )
                    .order_by(ProjectMessage.sequence.desc())
                    .limit(before_count + missing)
                )
            ).scalars().all()
        visible_rows = [*reversed(before_rows), included_message, *after_rows]
        first_sequence = visible_rows[0].sequence
        last_sequence = visible_rows[-1].sequence
        has_more_before = bool(
            first_sequence is not None
            and await session.scalar(
                select(ProjectMessage.id)
                .where(
                    *base_conditions,
                    ProjectMessage.sequence < first_sequence,
                )
                .limit(1)
            )
        )
        has_more_after = bool(
            last_sequence is not None
            and await session.scalar(
                select(ProjectMessage.id)
                .where(
                    *base_conditions,
                    ProjectMessage.sequence > last_sequence,
                )
                .limit(1)
            )
        )
        has_more = has_more_before or has_more_after
        centered_before_cursor = first_sequence if has_more_before else None
        centered_after_cursor = last_sequence if has_more_after else None
    elif after_sequence is not None:
        rows = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    *base_conditions,
                    ProjectMessage.sequence > after_sequence,
                )
                .order_by(ProjectMessage.sequence.asc())
                .limit(limit + 1)
            )
        ).scalars().all()
        has_more = len(rows) > limit
        visible_rows = list(rows[:limit])
    else:
        statement = select(ProjectMessage).where(*base_conditions)
        if before_sequence is not None:
            statement = statement.where(
                ProjectMessage.sequence < before_sequence
            )
        rows = (
            await session.execute(
                statement.order_by(
                    ProjectMessage.sequence.desc().nullslast(),
                    ProjectMessage.created_at.desc(),
                    ProjectMessage.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars().all()
        has_more = len(rows) > limit
        visible_rows = list(reversed(rows[:limit]))

    return StrategicConversationPage(
        rows=tuple(visible_rows),
        limit=limit,
        has_more=has_more,
        next_before_sequence=(
            centered_before_cursor
            if included_message is not None
            else (
                visible_rows[0].sequence
                if has_more
                and visible_rows
                and visible_rows[0].sequence is not None
                else None
            )
        ),
        next_after_sequence=(
            centered_after_cursor
            if included_message is not None
            else (
                visible_rows[-1].sequence
                if after_sequence is not None and visible_rows
                else None
            )
        ),
        target_found=True if include_message_id is not None else None,
    )
