"""Owner-controlled project qualification-start fence and exact receipts."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, StrictBool, StrictInt
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.release_qualification_control import (
    QualificationControlConflict,
    QualificationControlError,
    QualificationControlProjectNotFound,
    apply_qualification_control,
    qualification_control_readback,
)

router = APIRouter()


class QualificationControlBody(BaseModel):
    expected_revision: StrictInt = Field(ge=0)
    automatic_starts_enabled: StrictBool
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=500)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


@router.get("/projects/{project_id}/qualification-control")
async def get_qualification_control(
    project_id: uuid.UUID,
    response: Response,
    idempotency_key: str | None = Query(default=None, max_length=255),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    _no_store(response)
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="viewer"
    )
    try:
        return await qualification_control_readback(
            session, project_id, idempotency_key=idempotency_key
        )
    except QualificationControlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/projects/{project_id}/qualification-control")
async def set_qualification_control(
    project_id: uuid.UUID,
    payload: QualificationControlBody,
    response: Response,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    _no_store(response)
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, min_role="owner"
    )
    try:
        result = await apply_qualification_control(
            session,
            project_id=project_id,
            expected_revision=payload.expected_revision,
            automatic_starts_enabled=payload.automatic_starts_enabled,
            idempotency_key=payload.idempotency_key,
            actor_user_id=str(current_user["user_id"]),
            reason=payload.reason,
        )
        await session.commit()
        return result
    except QualificationControlConflict as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QualificationControlProjectNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QualificationControlError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
