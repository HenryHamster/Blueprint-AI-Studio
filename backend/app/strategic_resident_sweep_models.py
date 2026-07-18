"""Durable cursor state for bounded Strategic Resident producer sweeps."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import BigInteger, Column, String, func, text
from sqlmodel import Field, SQLModel
from sqlmodel.sql.sqltypes import GUID

from app.models_base import NaiveUTCDateTime
from app.time_utils import _utcnow


class StrategicResidentSweepState(SQLModel, table=True):
    """Durable high-water for one leader-owned Resident producer sweep."""

    sweep_name: str = Field(
        sa_column=Column(String(64), primary_key=True, nullable=False),
    )
    # Deliberately not a foreign key: deleting the last-inspected project must
    # not invalidate the round-robin position. The next range scan naturally
    # advances past a missing UUID and wraps when necessary.
    after_project_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(GUID(), nullable=True),
    )
    version: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )
