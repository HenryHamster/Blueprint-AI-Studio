"""Strategic check-in projection helpers.

Async DB-backed check-in cluster extracted from
routers/admin_project_strategic_session.py.
"""

import uuid
from datetime import datetime
from typing import Dict, Any
from sqlalchemy import and_, case, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import (
    Alert,
    MessageType,
    ProjectMessage,
    StrategicCycle,
    Task,
    TaskStatus,
    _utcnow,
)
from app.messages_noise_prefs_store import (
    get_messages_noise_prefs_row,
    mutate_messages_noise_prefs,
)
from app.message_convergence import LINKED_STATUS, OPEN_STATUS
from app.work_session_command_execution import OPERATOR_COMMAND_EVENT_TYPE
from app.strategic_state_read_model import _compact_text, _cycle_summary

_CHECK_IN_EVENT_TYPE = "strategic_check_in"
_CHECK_IN_PREFS_KEY = "_strategic_check_in"


async def _count_rows(session: AsyncSession, statement: Any) -> int:
    return int((await session.scalar(statement)) or 0)


def _sum_case(condition: Any) -> Any:
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def _work_session_last_check_in(message: ProjectMessage) -> Dict[str, Any]:
    context = message.context if isinstance(message.context, dict) else {}
    check_in = context.get("check_in") if isinstance(context, dict) else {}
    kind = check_in.get("kind") if isinstance(check_in, dict) else None
    return {
        "message_id": str(message.id),
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "content": message.content,
        "kind": _compact_text(kind, max_chars=40) or "manual",
    }


def _message_is_operator_check_in(message: ProjectMessage, user_id: str) -> bool:
    context = message.context if isinstance(message.context, dict) else {}
    check_in = context.get("check_in") if isinstance(context, dict) else {}
    return (
        context.get("event_type") == _CHECK_IN_EVENT_TYPE
        and isinstance(check_in, dict)
        and check_in.get("operator_user_id") == user_id
    )


async def _persist_strategic_check_in_marker(
    session: AsyncSession,
    *,
    user_id: str,
    project_id: uuid.UUID,
    message: ProjectMessage,
) -> None:
    context = message.context if isinstance(message.context, dict) else {}
    check_in = context.get("check_in") if isinstance(context, dict) else {}
    marker = {
        "version": 1,
        "message_id": str(message.id),
        "operator_user_id": user_id,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "kind": _compact_text(
            check_in.get("kind") if isinstance(check_in, dict) else None,
            max_chars=40,
        )
        or "manual",
    }

    def merge(existing_prefs: dict[str, Any]) -> dict[str, Any]:
        prefs = dict(existing_prefs)
        existing_marker = prefs.get(_CHECK_IN_PREFS_KEY)
        if isinstance(existing_marker, dict):
            existing_created_at = _parse_iso_datetime(existing_marker.get("created_at"))
            marker_created_at = _parse_iso_datetime(marker.get("created_at"))
            if (
                existing_created_at is not None
                and marker_created_at is not None
                and existing_created_at > marker_created_at
            ):
                return prefs
        prefs[_CHECK_IN_PREFS_KEY] = marker
        return prefs

    await mutate_messages_noise_prefs(session, user_id, project_id, merge)
    await session.commit()


async def _latest_strategic_check_in(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: str,
) -> ProjectMessage | None:
    prefs_row = await get_messages_noise_prefs_row(session, user_id, project_id)
    marker = (
        (prefs_row.prefs or {}).get(_CHECK_IN_PREFS_KEY)
        if prefs_row is not None
        else None
    )
    message_id = marker.get("message_id") if isinstance(marker, dict) else None
    if not isinstance(message_id, str):
        return None
    try:
        parsed_message_id = uuid.UUID(message_id)
    except ValueError:
        return None
    message = await session.get(ProjectMessage, parsed_message_id)
    if message is None or message.project_id != project_id:
        return None
    return message if _message_is_operator_check_in(message, user_id) else None


async def _work_session_since_last_check_in(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    since: Any,
    until: Any,
) -> Dict[str, Any]:
    message_event_type = ProjectMessage.context["event_type"].as_string()
    message_since_filter = [
        ProjectMessage.project_id == project_id,
        ProjectMessage.created_at > since,
        ProjectMessage.created_at <= until,
        or_(
            message_event_type.is_(None),
            message_event_type.notin_(
                [_CHECK_IN_EVENT_TYPE, OPERATOR_COMMAND_EVENT_TYPE]
            ),
        ),
    ]
    task_since_filter = [
        Task.project_id == project_id,
        Task.archived.is_(False),
    ]
    alert_since_filter = [
        Alert.project_id == project_id,
        Alert.created_at > since,
        Alert.created_at <= until,
    ]
    completed_cycle_at = func.coalesce(
        StrategicCycle.phase2_completed_at,
        StrategicCycle.phase1_completed_at,
        StrategicCycle.updated_at,
    )
    completed_cycle_filter = [
        StrategicCycle.project_id == project_id,
        StrategicCycle.outcome == "completed",
        completed_cycle_at > since,
        completed_cycle_at <= until,
    ]
    message_counts = (
        await session.execute(
            select(
                func.count(),
                _sum_case(ProjectMessage.from_type.in_(["user", "operator"])),
                _sum_case(
                    ProjectMessage.from_type.in_(
                        ["strategic", "strategist", "system"]
                    )
                ),
                _sum_case(
                    and_(
                        ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
                        ProjectMessage.message_type.in_(
                            [
                                MessageType.QUESTION,
                                MessageType.BLOCKER,
                                MessageType.BUG_REPORT,
                            ]
                        ),
                    )
                ),
                _sum_case(
                    and_(
                        ProjectMessage.status.in_([OPEN_STATUS, LINKED_STATUS]),
                        ProjectMessage.message_type == MessageType.FEEDBACK,
                    )
                ),
            ).where(*message_since_filter)
        )
    ).one()
    task_window_filter = or_(
        and_(Task.created_at > since, Task.created_at <= until),
        and_(Task.updated_at > since, Task.updated_at <= until),
        and_(Task.completed_at > since, Task.completed_at <= until),
    )
    task_counts = (
        await session.execute(
            select(
                _sum_case(and_(Task.created_at > since, Task.created_at <= until)),
                _sum_case(and_(Task.updated_at > since, Task.updated_at <= until)),
                _sum_case(
                    and_(Task.completed_at > since, Task.completed_at <= until)
                ),
                _sum_case(
                    and_(
                        Task.updated_at > since,
                        Task.updated_at <= until,
                        Task.status == TaskStatus.FAILED,
                    )
                ),
                _sum_case(
                    and_(
                        Task.updated_at > since,
                        Task.updated_at <= until,
                        Task.status == TaskStatus.BLOCKED,
                    )
                ),
                _sum_case(
                    and_(
                        Task.updated_at > since,
                        Task.updated_at <= until,
                        Task.status == TaskStatus.PARKED,
                    )
                ),
            ).where(*task_since_filter, task_window_filter)
        )
    ).one()
    alert_counts = (
        await session.execute(
            select(
                func.count(),
                _sum_case(Alert.dismissed_at.is_(None)),
                _sum_case(Alert.severity == "ERROR"),
                _sum_case(Alert.severity == "WARNING"),
            ).where(*alert_since_filter)
        )
    ).one()
    strategic_count = await _count_rows(
        session,
        select(func.count()).select_from(StrategicCycle).where(*completed_cycle_filter),
    )
    latest_cycle = (
        await session.execute(
            select(StrategicCycle)
            .where(*completed_cycle_filter)
            .order_by(completed_cycle_at.desc())
            .limit(1)
        )
    ).scalars().first()
    return {
        "version": 1,
        "available": True,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "messages": {
            "total": int(message_counts[0] or 0),
            "operator": int(message_counts[1] or 0),
            "strategic": int(message_counts[2] or 0),
            "needs_operator": int(message_counts[3] or 0),
            "watch": int(message_counts[4] or 0),
        },
        "tasks": {
            "created": int(task_counts[0] or 0),
            "updated": int(task_counts[1] or 0),
            "completed": int(task_counts[2] or 0),
            "failed": int(task_counts[3] or 0),
            "blocked": int(task_counts[4] or 0),
            "parked": int(task_counts[5] or 0),
        },
        "alerts": {
            "raised": int(alert_counts[0] or 0),
            "active": int(alert_counts[1] or 0),
            "errors": int(alert_counts[2] or 0),
            "warnings": int(alert_counts[3] or 0),
        },
        "strategic": {
            "cycles_completed": strategic_count,
            "latest_cycle": _cycle_summary(latest_cycle),
        },
    }


async def _build_work_session_check_in_projection(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: str,
) -> Dict[str, Any]:
    now = _utcnow()
    latest = await _latest_strategic_check_in(session, project_id, user_id)
    if latest is None:
        return {
            "last_check_in": None,
            "since_last_check_in": {
                "version": 1,
                "available": False,
                "reason": "no_prior_check_in",
                "since": None,
                "until": now.isoformat(),
            },
        }
    return {
        "last_check_in": _work_session_last_check_in(latest),
        "since_last_check_in": await _work_session_since_last_check_in(
            session,
            project_id,
            since=latest.created_at,
            until=now,
        ),
    }
