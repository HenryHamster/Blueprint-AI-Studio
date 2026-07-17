"""Durable control-plane models for the per-project Strategic Resident.

Kept separate from :mod:`app.models` because that module is already at its
size guardrail.  ``app.models`` imports and re-exports these classes so existing
model discovery and ``SQLModel.metadata`` registration keep working.
"""

from __future__ import annotations

from typing import Any
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel
from sqlmodel.sql.sqltypes import GUID

from app.models_base import NaiveUTCDateTime
from app.time_utils import _utcnow


class StrategicResident(SQLModel, table=True):
    """One durable Resident control-plane row per project."""

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    engine_owner: str = Field(
        default="legacy",
        sa_column=Column(String(32), nullable=False, server_default=text("'legacy'")),
    )
    engine_version: str = Field(
        default="legacy-v1",
        sa_column=Column(
            String(64), nullable=False, server_default=text("'legacy-v1'")
        ),
    )
    background_policy: str = Field(
        default="active",
        sa_column=Column(String(16), nullable=False, server_default=text("'active'")),
    )
    authority_generation: int = Field(
        default=1,
        sa_column=Column(BigInteger, nullable=False, server_default=text("1")),
    )

    # Exactly one turn may hold mutation authority project-wide.  Interactive
    # and background each retain at most one coalesced successor.
    active_turn_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
        ),
    )
    active_turn_lane: str | None = Field(
        default=None, sa_column=Column(String(16), nullable=True)
    )
    interactive_successor_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
        ),
    )
    background_successor_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(), ForeignKey("task.id", ondelete="SET NULL"), nullable=True
        ),
    )

    current_checkpoint_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey(
                "strategicresidentcheckpoint.id",
                name="fk_strategicresident_current_checkpoint",
                ondelete="SET NULL",
                use_alter=True,
            ),
            nullable=True,
        ),
    )
    previous_checkpoint_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey(
                "strategicresidentcheckpoint.id",
                name="fk_strategicresident_previous_checkpoint",
                ondelete="SET NULL",
                use_alter=True,
            ),
            nullable=True,
        ),
    )

    handled_message_sequence: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, server_default=text("0")),
    )
    handled_project_change_cursor: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    runner_policy: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    provider_policy: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    failover_policy: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    health_status: str = Field(
        default="unknown",
        sa_column=Column(String(24), nullable=False, server_default=text("'unknown'")),
    )
    health_state: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    recovery_state: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    scheduling_state: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    rollout_state: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    next_wakeup_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    last_wakeup_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    last_healthy_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    last_error_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
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

    __table_args__ = (
        CheckConstraint(
            "engine_owner IN ('legacy','strategic_resident')",
            name="chk_strategicresident_engine_owner",
        ),
        CheckConstraint(
            "background_policy IN ('active','paused')",
            name="chk_strategicresident_background_policy",
        ),
        CheckConstraint(
            "authority_generation >= 1",
            name="chk_strategicresident_authority_generation",
        ),
        CheckConstraint(
            "active_turn_lane IS NULL OR active_turn_lane IN ('interactive','background')",
            name="chk_strategicresident_active_lane",
        ),
        CheckConstraint(
            "(active_turn_task_id IS NULL AND active_turn_lane IS NULL) OR "
            "(active_turn_task_id IS NOT NULL AND active_turn_lane IS NOT NULL)",
            name="chk_strategicresident_active_pair",
        ),
        CheckConstraint(
            "active_turn_task_id IS NULL OR interactive_successor_task_id IS NULL "
            "OR active_turn_task_id <> interactive_successor_task_id",
            name="chk_strategicresident_active_interactive_distinct",
        ),
        CheckConstraint(
            "active_turn_task_id IS NULL OR background_successor_task_id IS NULL "
            "OR active_turn_task_id <> background_successor_task_id",
            name="chk_strategicresident_active_background_distinct",
        ),
        CheckConstraint(
            "interactive_successor_task_id IS NULL OR "
            "background_successor_task_id IS NULL OR "
            "interactive_successor_task_id <> background_successor_task_id",
            name="chk_strategicresident_successors_distinct",
        ),
        CheckConstraint(
            "current_checkpoint_id IS NULL OR previous_checkpoint_id IS NULL OR "
            "current_checkpoint_id <> previous_checkpoint_id",
            name="chk_strategicresident_checkpoints_distinct",
        ),
        CheckConstraint(
            "handled_message_sequence >= 0",
            name="chk_strategicresident_message_cursor",
        ),
        CheckConstraint(
            "health_status IN ('unknown','healthy','degraded','recovering','blocked')",
            name="chk_strategicresident_health_status",
        ),
        Index(
            "ix_strategicresident_scheduler",
            "next_wakeup_at",
            "project_id",
            postgresql_where=text(
                "engine_owner = 'strategic_resident' AND background_policy = 'active' "
                "AND next_wakeup_at IS NOT NULL"
            ),
            sqlite_where=text(
                "engine_owner = 'strategic_resident' AND background_policy = 'active' "
                "AND next_wakeup_at IS NOT NULL"
            ),
        ),
        Index("ix_strategicresident_owner_health", "engine_owner", "health_status"),
        Index("ix_strategicresident_active_turn_task", "active_turn_task_id"),
        Index(
            "ix_strategicresident_interactive_successor",
            "interactive_successor_task_id",
        ),
        Index(
            "ix_strategicresident_background_successor",
            "background_successor_task_id",
        ),
    )


class StrategicResidentCheckpoint(SQLModel, table=True):
    """Immutable, content-addressed Resident checkpoint."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("strategicresident.project_id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    version: int = Field(sa_column=Column(BigInteger, nullable=False))
    schema_version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    content_hash: str = Field(sa_column=Column(String(64), nullable=False))
    checkpoint: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    authority_generation: int = Field(
        default=1,
        sa_column=Column(BigInteger, nullable=False, server_default=text("1")),
    )
    source_turn_task_id: uuid.UUID | None = Field(
        default=None,
        # Durable provenance identity without a Task FK: checkpoint immutability
        # and historical attribution must survive Task cleanup.
        sa_column=Column(GUID(), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "version",
            name="uq_strategicresidentcheckpoint_project_version",
        ),
        CheckConstraint("version > 0", name="chk_strategicresidentcheckpoint_version"),
        CheckConstraint(
            "schema_version > 0",
            name="chk_strategicresidentcheckpoint_schema_version",
        ),
        CheckConstraint(
            "length(content_hash) = 64",
            name="chk_strategicresidentcheckpoint_hash",
        ),
        CheckConstraint(
            "authority_generation >= 1",
            name="chk_strategicresidentcheckpoint_authority_generation",
        ),
        CheckConstraint(
            "(version = 1 AND source_turn_task_id IS NULL) OR "
            "(version > 1 AND source_turn_task_id IS NOT NULL)",
            name="chk_strategicresidentcheckpoint_source_version",
        ),
        Index(
            "ix_strategicresidentcheckpoint_project_created",
            "project_id",
            "created_at",
        ),
    )


class StrategicOperationReceipt(SQLModel, table=True):
    """Durable idempotency and reconciliation record for a Resident mutation."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("strategicresident.project_id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    # Intentionally not a FK: Task cleanup is allowed to delete old tasks, but a
    # receipt must retain the exact authority identity that produced the effect.
    turn_task_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    authority_generation: int = Field(sa_column=Column(BigInteger, nullable=False))
    operation_id: str = Field(sa_column=Column(String(128), nullable=False))
    operation_kind: str = Field(sa_column=Column(String(64), nullable=False))
    capability: str = Field(sa_column=Column(String(64), nullable=False))
    risk_level: str = Field(
        default="low",
        sa_column=Column(String(16), nullable=False, server_default=text("'low'")),
    )
    request_hash: str = Field(sa_column=Column(String(64), nullable=False))
    expected_revisions: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    observed_revisions_before: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    observed_revisions_after: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    status: str = Field(
        default="pending",
        sa_column=Column(String(24), nullable=False, server_default=text("'pending'")),
    )
    receipt: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    unknown_effects: list[Any] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    error_code: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    error_detail: str | None = Field(
        default=None, sa_column=Column(String(1000), nullable=True)
    )
    started_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
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
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(NaiveUTCDateTime, nullable=True)
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "operation_id",
            name="uq_strategicoperationreceipt_project_operation",
        ),
        CheckConstraint(
            "authority_generation >= 1",
            name="chk_strategicoperationreceipt_authority_generation",
        ),
        CheckConstraint(
            "risk_level IN ('low','medium','high','critical')",
            name="chk_strategicoperationreceipt_risk_level",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="chk_strategicoperationreceipt_request_hash",
        ),
        CheckConstraint(
            "status IN ('pending','applying','applied','reconciling',"
            "'reconciled','failed','unknown_effect')",
            name="chk_strategicoperationreceipt_status",
        ),
        Index(
            "ix_strategicoperationreceipt_project_status_updated",
            "project_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_strategicoperationreceipt_task_started",
            "turn_task_id",
            "started_at",
        ),
    )
