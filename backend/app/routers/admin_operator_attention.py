"""Operator attention projection endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project
from app.auth import get_current_user
from app.database import get_session
from app.operator_attention import build_operator_attention

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-operator-attention"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/operator-attention")
async def get_operator_attention(
    project_id: str = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a read-only attention projection for the selected project."""
    try:
        pid = uuid.UUID(project_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id format")
    project = await _get_user_project(pid, current_user["user_id"], session)
    return await build_operator_attention(session, project, current_user["user_id"])
