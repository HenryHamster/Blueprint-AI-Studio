"""Bounded direction catalog for scoped service credentials."""

from datetime import datetime, timezone
import os
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_session
from app.models import Project
from app.routers.agent_access import (
    _AGENT_LIST_ITEM_MAX_BYTES,
    _AGENT_LIST_RESPONSE_MAX_BYTES,
    _AGENT_TASK_NESTED_COLLECTION_LIMIT,
    _AGENT_TASK_NESTED_DEPTH_LIMIT,
    _AGENT_TASK_STRING_MAX_BYTES,
    _enforce_agent_list_response_size,
    _get_user_project,
    _project_agent_list_items,
    _project_agent_list_project,
)
from app.routers.directions import get_directions


router = APIRouter(prefix="/api/agent-access/v1", tags=["agent-access"])


def _project_agent_direction_list(
    raw_response: dict[str, Any],
    project: Project | dict[str, Any],
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    item_fields = (
        "id",
        "text",
        "priority",
        "status",
        "paths",
        "added_at",
        "category",
        "completion_criteria",
        "cost_budget_usd",
        "staged_for_version",
        "staged_at",
        "held",
        "held_reason",
        "held_at",
        "held_until",
        "refractory_until",
        "refractory_reason",
        "possibly_complete",
        "possibly_saturated",
        "pending_update",
        "needs_manual_intervention",
        "quality_score",
        "merges_per_cycle",
        "generalized_successes",
        "generalized_health_rate",
        "knowledge_document_count",
        "context",
        "research_context",
        "research_history",
        "notes",
    )
    directions, items_truncated, fields_truncated, redactions, source_returned = (
        _project_agent_list_items(
            raw_response.get("directions"),
            fields=item_fields,
            section="task_list_item",
            limit=limit,
        )
    )
    project_projection, project_truncated, project_redactions = (
        _project_agent_list_project(project)
    )
    redactions += project_redactions
    total = int(raw_response.get("total") or 0)
    page_incomplete = total > offset + source_returned
    items_incomplete = items_truncated or source_returned > len(directions)
    truncation = {
        "source_page": page_incomplete,
        "items": items_incomplete,
        "item_fields": fields_truncated or project_truncated,
        "response_bytes": False,
    }
    response = {
        "schema_version": "blueprint.agent-directions.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "complete": not any(truncation.values()),
        "project": project_projection,
        "limits": {
            "response_bytes": _AGENT_LIST_RESPONSE_MAX_BYTES,
            "maximum_limit": 100,
            "requested_limit": limit,
            "item_value_budget_bytes": _AGENT_LIST_ITEM_MAX_BYTES,
            "string_serialized_bytes": _AGENT_TASK_STRING_MAX_BYTES,
            "nested_depth": _AGENT_TASK_NESTED_DEPTH_LIMIT,
            "nested_collection_entries": _AGENT_TASK_NESTED_COLLECTION_LIMIT,
        },
        "truncation": truncation,
        "redaction": {
            "applied": redactions > 0,
            "fields_or_values": redactions,
            "policy": "credential fields and credential-like string values are removed",
        },
        "directions": directions,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(directions),
        "source_returned": source_returned,
        "has_more": page_incomplete or items_incomplete,
        "next_offset": (
            offset + len(directions) if page_incomplete or items_incomplete else None
        ),
        "server": {"revision": os.getenv("K_REVISION", "local-dev")},
    }
    _enforce_agent_list_response_size(response, item_key="directions")
    return response


@router.get("/directions")
async def list_agent_directions(
    project_id: uuid.UUID = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return one bounded page of project directions without rich project state."""
    project = await _get_user_project(project_id, current_user["user_id"], session)
    raw_response = await get_directions(
        project_id=project_id,
        session=session,
        current_user=current_user,
        lite=True,
    )
    all_directions = list(raw_response.get("directions") or [])
    page = all_directions[offset : offset + limit]
    return _project_agent_direction_list(
        {"directions": page, "total": len(all_directions)},
        project,
        limit=limit,
        offset=offset,
    )
