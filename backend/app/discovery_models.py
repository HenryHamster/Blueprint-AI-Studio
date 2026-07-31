"""Durable persistence models for evidence-bearing discovery review cycles.

This module owns the small, authoritative state machine that groups a bounded
cohort of normal Blueprint tasks around one research/product decision.  It is
kept separate from :mod:`app.models` for the same reason as
``app.release_models``: the domain is cohesive, and importing it from
``app.models`` registers its tables on the shared ``SQLModel.metadata`` without
creating an application-module dependency cycle.

The evidence ledger stores immutable references, checksums, and provenance
rather than duplicating artifact bodies.  ``semantic_role`` and ``source_kind``
are intentionally open strings validated at the service boundary; evidence
formats can evolve without requiring a schema migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel
from sqlmodel.sql.sqltypes import GUID

from app.models_base import NaiveUTCDateTime
from app.time_utils import _utcnow


_LIVE_DISCOVERY_STATES = (
    "open",
    "admission_closed",
    "review_pending",
    "reviewing",
    "review_blocked",
)
_LIVE_DISCOVERY_STATE_SQL = (
    "state IN ('open','admission_closed','review_pending','reviewing',"
    "'review_blocked')"
)


class DiscoveryCycle(SQLModel, table=True):
    """One durable decision cycle for an uncertain strategic direction.

    ``direction_key`` is a snapshot of the direction's string id rather than a
    foreign key to ``direction``.  Direction persistence is still dual-written
    between ``Project.directions`` and the normalized table, and review history
    must survive a direction being pruned.
    """

    __tablename__ = "discoverycycle"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "direction_key",
            "generation",
            name="uq_discoverycycle_project_direction_generation",
        ),
        Index(
            "ux_discoverycycle_live_direction",
            "project_id",
            "direction_key",
            unique=True,
            postgresql_where=text(_LIVE_DISCOVERY_STATE_SQL),
            sqlite_where=text(_LIVE_DISCOVERY_STATE_SQL),
        ),
        Index(
            "ix_discoverycycle_state_deadline",
            "state",
            "deadline_at",
        ),
        CheckConstraint(
            "outcome_kind IN ('empirical','experiential','mixed')",
            name="chk_discoverycycle_outcome_kind",
        ),
        CheckConstraint(
            "phase IN ('probe','demonstrate','productize','release')",
            name="chk_discoverycycle_phase",
        ),
        CheckConstraint(
            "state IN ('open','admission_closed','review_pending','reviewing',"
            "'review_blocked','reviewed','superseded','cancelled')",
            name="chk_discoverycycle_state",
        ),
        CheckConstraint(
            "verdict IS NULL OR verdict IN "
            "('renew','pivot','expand','branch','fallback_pending','park','stop',"
            "'ask_operator','inconclusive')",
            name="chk_discoverycycle_verdict",
        ),
        CheckConstraint("generation >= 1", name="chk_discoverycycle_generation"),
        CheckConstraint("revision >= 0", name="chk_discoverycycle_revision"),
        CheckConstraint(
            "task_credit_limit IS NULL OR task_credit_limit > 0",
            name="chk_discoverycycle_task_credit_limit",
        ),
        CheckConstraint(
            "attempt_credit_limit IS NULL OR attempt_credit_limit > 0",
            name="chk_discoverycycle_attempt_credit_limit",
        ),
        CheckConstraint(
            "concurrency_limit IS NULL OR concurrency_limit > 0",
            name="chk_discoverycycle_concurrency_limit",
        ),
        CheckConstraint(
            "reserved_task_credits >= 0 AND consumed_task_credits >= 0 "
            "AND reserved_attempt_credits >= 0 AND consumed_attempt_credits >= 0",
            name="chk_discoverycycle_credit_counts",
        ),
        CheckConstraint(
            "mechanism_fingerprint IS NULL OR length(mechanism_fingerprint) = 64",
            name="chk_discoverycycle_mechanism_fingerprint",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    direction_key: str = Field(sa_column=Column(String(128), nullable=False))
    generation: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    outcome_kind: str = Field(sa_column=Column(String(24), nullable=False))
    phase: str = Field(
        default="probe",
        sa_column=Column(
            String(24),
            nullable=False,
            server_default=text("'probe'"),
        ),
    )
    state: str = Field(
        default="open",
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'open'"),
        ),
    )

    intent_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    bet_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    mechanism_fingerprint: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )
    candidate_sha: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    source_research_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    review_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

    task_credit_limit: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    attempt_credit_limit: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    concurrency_limit: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    reserved_task_credits: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    consumed_task_credits: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    reserved_attempt_credits: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    consumed_attempt_credits: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    deadline_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    verdict: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )
    review_result: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    reviewed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    revision: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
        ),
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


class DiscoveryEvidenceItem(SQLModel, table=True):
    """Append-only reference to one item in a cycle's review packet."""

    __tablename__ = "discoveryevidenceitem"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "dedupe_key",
            name="uq_discoveryevidenceitem_cycle_dedupe",
        ),
        Index(
            "ix_discoveryevidenceitem_cycle_role_created",
            "cycle_id",
            "semantic_role",
            "created_at",
        ),
        Index(
            "ix_discoveryevidenceitem_source",
            "source_kind",
            "source_id",
        ),
        CheckConstraint(
            "length(checksum) = 64",
            name="chk_discoveryevidenceitem_checksum",
        ),
        CheckConstraint(
            "mechanism_fingerprint IS NULL OR length(mechanism_fingerprint) = 64",
            name="chk_discoveryevidenceitem_mechanism_fingerprint",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    cycle_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("discoverycycle.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    semantic_role: str = Field(sa_column=Column(String(32), nullable=False))
    source_kind: str = Field(sa_column=Column(String(32), nullable=False))
    source_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )
    source_uri: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    checksum: str = Field(sa_column=Column(String(64), nullable=False))
    content_type: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )
    head_sha: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )
    mechanism_fingerprint: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )
    evidence_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    dedupe_key: str = Field(sa_column=Column(String(512), nullable=False))
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
        ),
    )


class DiscoveryAdmission(SQLModel, table=True):
    """Idempotent credit reservation for one cycle-scoped unit of work.

    A caller-selected ``admission_key`` makes retries converge on the same
    ledger row. The service updates ``state`` as the reservation is consumed
    or released; rows themselves remain as the durable accounting trail.
    """

    __tablename__ = "discoveryadmission"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "admission_key",
            name="uq_discoveryadmission_cycle_key",
        ),
        Index(
            "ix_discoveryadmission_cycle_state_created",
            "cycle_id",
            "state",
            "created_at",
        ),
        Index("ix_discoveryadmission_task_id", "task_id"),
        Index(
            "ix_discoveryadmission_spawn_request_id",
            "spawn_request_id",
        ),
        CheckConstraint(
            "admission_kind IN ('logical_task','attempt')",
            name="chk_discoveryadmission_kind",
        ),
        CheckConstraint(
            "state IN ('reserved','consumed','released')",
            name="chk_discoveryadmission_state",
        ),
        CheckConstraint("units > 0", name="chk_discoveryadmission_units"),
        CheckConstraint(
            "length(admission_key) > 0",
            name="chk_discoveryadmission_key_nonempty",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    cycle_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("discoverycycle.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    admission_key: str = Field(sa_column=Column(String(512), nullable=False))
    admission_kind: str = Field(sa_column=Column(String(24), nullable=False))
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    spawn_request_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("strategicspawnrequest.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    units: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )
    state: str = Field(
        default="reserved",
        sa_column=Column(
            String(24),
            nullable=False,
            server_default=text("'reserved'"),
        ),
    )
    admission_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
        ),
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
