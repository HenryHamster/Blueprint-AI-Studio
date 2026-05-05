from enum import Enum
from typing import Optional, Dict, Any, List
from sqlmodel import SQLModel, Field, Relationship
from sqlmodel.sql.sqltypes import GUID
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, String, Integer, Float, Index, Text, UniqueConstraint, ForeignKey, text, event, func, Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator
import logging
import uuid
from datetime import datetime, timezone
from app.time_utils import _utcnow


class NaiveUTCDateTime(TypeDecorator):
    """TIMESTAMPTZ in DB, naive UTC datetime in Python.

    Phase D2 (alembic 0061) migrated several columns to TIMESTAMPTZ for
    server-default support and clock-skew safety, but the rest of the codebase
    uses naive UTC throughout (_utcnow() returns naive). Rather than sweep
    every comparison site to handle aware datetimes, strip tz on read.

    Round 3 audit (M1): symmetric write path. Future tz-aware writers
    (e.g. anything calling ``datetime.now(timezone.utc)`` instead of
    ``_utcnow()``) get normalized to naive UTC on the way to the DB so the
    column stays internally consistent. asyncpg sends naive datetimes to
    TIMESTAMPTZ columns and Postgres interprets them as UTC.

    Pairs with the F1 helper rename _to_naive_utc -> _strip_tz_assume_utc;
    after this decorator lands, ORM-loaded datetimes are already naive and
    callsite strips become no-ops (audit 2026-04-29 round 2 / C3 + H1).
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

logger = logging.getLogger(__name__)


class ProjectStatus(str, Enum):
    DRAFT = "DRAFT"
    DEPLOYING = "DEPLOYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    IN_PROGRESS = "IN_PROGRESS"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class ArtifactType(str, Enum):
    GIT_COMMIT = "GIT_COMMIT"
    PULL_REQUEST = "PULL_REQUEST"
    SCREENSHOT = "SCREENSHOT"
    ERROR_LOG = "ERROR_LOG"
    RESEARCH_FINDINGS = "RESEARCH_FINDINGS"
    SECURITY_REPORT = "SECURITY_REPORT"
    SMOKETEST_RESULT = "SMOKETEST_RESULT"
    DEBUG_RESULT = "DEBUG_RESULT"
    PERF_REPORT = "PERF_REPORT"
    INVESTIGATION_REPORT = "INVESTIGATION_REPORT"
    SELF_AUDIT_REPORT = "SELF_AUDIT_REPORT"


class LLMProviderName(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


class LLMAuthMode(str, Enum):
    OAUTH = "oauth"
    API_KEY = "api_key"
    SERVICE_ACCOUNT = "service_account"


# --- Auto-pause diagnosis workflow constants ---
# Shared values for the direction auto-pause -> investigate task spawn flow.
# Kept here so direction_lifecycle, anomaly_detection, task_completion, and
# strategic_completion all agree on the same thresholds and key names.

# Minimum hours between successive diagnosis investigate tasks for the same
# direction. Prevents a flood of investigate spawns when a direction keeps
# failing health checks on every autonomous tick.
DIAGNOSIS_DEDUP_WINDOW_HOURS = 24

# Direction dict keys for diagnosis state. The `directions` JSONB field on
# Project stores these alongside existing pause keys.
#   DIAGNOSIS_IN_PROGRESS_KEY: bool — true while an investigate task is open
#   DIAGNOSIS_TASK_ID_KEY:     str  — UUID of the spawned investigate task
#   DIAGNOSIS_STARTED_AT_KEY:  str  — ISO datetime of spawn (for dedup window)
DIAGNOSIS_IN_PROGRESS_KEY = "diagnosis_in_progress"
DIAGNOSIS_TASK_ID_KEY = "diagnosis_task_id"
DIAGNOSIS_STARTED_AT_KEY = "diagnosis_started_at"

# Direction recommended_mode provenance metadata (stored in Project.directions JSONB).
RECOMMENDED_MODE_META_KEY = "recommended_mode_meta"
RECOMMENDED_MODE_SOURCE_STRATEGIC = "strategic"
RECOMMENDED_MODE_SOURCE_SELF_AUDIT = "self_audit"


# --- Models ---

class AllowedUser(SQLModel, table=True):
    """
    Allowlist of users permitted to access the Blueprint application.
    Roles are managed here instead of Firebase custom claims.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(index=True, unique=True)
    firebase_uid: Optional[str] = Field(default=None, index=True)
    # Allowed values: 'admin' | 'user'. Enforced server-side by
    # ``chk_alloweduser_role`` (alembic 0065 / audit L-S1, Postgres-only).
    role: str = Field(default="user")
    display_name: Optional[str] = None
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5):
    # default_factory is retained as an additive Python-side guard so ORM
    # callers don't see ``None`` between construction and commit; the
    # server_default protects raw-SQL INSERT paths that bypass the ORM.
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    last_login_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )


class UserIntegration(SQLModel, table=True):
    """
    Stores third-party integrations for a user, such as GitHub App installations.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # ``user_id`` previously had ``index=True`` (yielding the unique index
    # ``ix_userintegration_user_id`` from migration 0001) — superseded by the
    # named ``uq_userintegration_user_id`` UNIQUE constraint added in 0049 and
    # dropped in 0060 (audit 2026-04-29 / H-S3). The constraint's underlying
    # index is the canonical lookup path; do not re-add ``index=True`` here.
    user_id: str = Field()

    # GitHub App Installation ID
    github_installation_id: Optional[str] = None
    github_account_name: Optional[str] = None

    # GitHub Fine-Grained Personal Access Token (Alternative to App Installation)
    github_pat: Optional[str] = None

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_userintegration_user_id"),
    )

class Project(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    # Firebase UID of the project owner. Intentionally NOT a FK to
    # ``alloweduser.firebase_uid`` (audit 2026-04-29 / L-S2): a Project may
    # be created via an admin token whose Firebase UID has not yet been
    # populated on the matching ``alloweduser`` row (firebase_uid is filled
    # on first login, but project creation can precede first login during
    # bootstrap / onboarding). A FK here would force a hard ordering
    # between allowlist seeding and project creation that the bootstrap
    # flow doesn't otherwise need. Loose-coupled by design.
    user_id: str = Field(index=True)
    status: ProjectStatus = Field(default=ProjectStatus.DRAFT, sa_column=Column(String, default=ProjectStatus.DRAFT))
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # Autonomous improvement: user-defined high-level directions for research agents
    # Includes directions with status: 'active', 'paused', 'proposed', 'rejected', 'expired'
    # Auto-pause diagnosis keys (set when an investigate task is spawned to
    # diagnose repeated code failures — see DIAGNOSIS_* constants above):
    #   diagnosis_in_progress: bool, diagnosis_task_id: str, diagnosis_started_at: ISO str
    directions: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, default=[]))
    # Strategic product vision maintained by the strategic research agent.
    # Legacy: being replaced by ``problem_model`` (see below). During migration
    # we render vision from the problem model; once all consumers migrate, this
    # column becomes a cache or is removed.
    vision: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=True, default={}))
    # Structured problem model — the strategic layer's durable understanding.
    # Replaces vision + design_doc/notes/strategic-context as the source of
    # truth for strategic state. Shape defined in app.problem_model.ProblemModel.
    problem_model: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=True, default={}))
    # Self-reflection log — append-only list of the strategic agent's
    # own observations about what was hard / what context it was missing
    # each cycle. Used on self-repo projects to drive the self-improvement
    # loop. Empty list on non-self-repo projects. Entry shape defined in
    # app.problem_model.SelfReflectionEntry; cap SELF_REFLECTION_LOG_MAX.
    self_reflection_log: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=True, default=[]),
    )
    # Running update — a single markdown document the strategic agent
    # maintains as a living narrative of what the project is and what's
    # shifting. Rewritten each cycle (not appended) so the user always
    # sees a clean current view, not a log of per-cycle summaries. User
    # acknowledges to stamp running_update_acknowledged_at; the document
    # persists and the agent refines it on the next cycle.
    running_update: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    running_update_acknowledged_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Living architectural description maintained by design doc update agent
    design_doc: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=True, default={}))
    # Toggle autonomous research/task generation
    autonomous_enabled: bool = Field(default=True)
    # Self-repo flag: enables rollback monitoring, extended windows, infra soft gates
    # Set True when github_owner/github_repo matches Blueprint's own repo
    is_self_repo: bool = Field(default=False)
    # Build configuration: directories, commands, git identity for the agent
    build_config: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Project-level CI auto-remediation outcomes for fixes that could not be
    # linked to a direction (novel failures whose fingerprint matched no
    # direction). Shape: {"attempts": int, "last_result": str, "last_at": iso,
    # "history": [{task_id, result, timestamp, fingerprint, failure_class}]}.
    remediation_tracking: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))

    tasks: List["Task"] = Relationship(
        back_populates="project",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class ProjectMember(SQLModel, table=True):
    """
    Many-to-many membership linking users to projects with roles.
    Roles: 'owner' (full control + invites), 'member' (tasks + directions), 'viewer' (read-only).
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    user_id: str = Field(index=True)
    # Allowed values: 'owner' | 'member' | 'viewer'. Enforced server-side by
    # ``chk_projectmember_role`` (alembic 0065 / audit L-S1, Postgres-only).
    role: str = Field(default="member")
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )


class Task(SQLModel, table=True):
    """
    Extensible Task model. The 'payloads' JSONB column holds a flexible list
    of structured dictionaries defining the work (e.g., UI components, API routes).
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)

    # Multi-Agent & Hierarchy Fields
    title: str = Field(default="Untitled Task")
    description: str = Field(default="")
    parent_task_id: Optional[uuid.UUID] = Field(default=None, foreign_key="task.id", index=True)
    latest_status_message: Optional[str] = None
    status_log: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")))

    # Task provenance. Allowed canonical values:
    #   'USER' | 'ORCHESTRATOR' | 'STRATEGIC_AGENT' |
    #   'SYSTEM_AUTO_REMEDIATION' | 'SYSTEM_AUTO_REVIEW' |
    #   'SYSTEM_ISSUE_IMPLEMENTATION'
    # Test fixtures additionally use 'TEST' / 'test-user' / 'user' / 'user1'
    # — these are accepted by the CHECK constraint but should not be
    # introduced in production code paths. Enforced server-side by
    # ``chk_task_created_by`` (alembic 0065 / audit L-S1, Postgres-only).
    created_by: str = Field(default="USER")
    launched_by_user_id: Optional[str] = Field(default=None)  # Firebase UID of the user who launched this task (for launcher-pays billing)
    idempotency_key: Optional[str] = Field(default=None, sa_column=Column(String(255), nullable=True))
    requires_approval: bool = Field(default=False)

    status: TaskStatus = Field(default=TaskStatus.QUEUED, sa_column=Column(String, default=TaskStatus.QUEUED))

    # Push Model: Orchestrator assigns this task to a specific VM
    vm_instance_id: Optional[str] = Field(default=None, index=True)

    # Task dependency DAG: list of task UUIDs that must be COMPLETED+merged before dispatch
    depends_on: List[str] = Field(default_factory=list, sa_column=Column(JSONB, default=[]))

    # Flexible Payloads: A list of structured dictionaries defining the work.
    # E.g., [{"interface": "ui_component_v1", "data": {...}}]
    payloads: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")))

    # Shared memory/context for the agent during execution
    context: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")))

    # Per-task fix-loop telemetry. Shape:
    #   {"lint": [FixLoopAttempt, ...], "test": [...], "check": [...]}
    # Each FixLoopAttempt is a loose dict validated at the ingest boundary by
    # ``app.fix_loop_telemetry.FixLoopEventIn`` (loop, iteration, fingerprint_hash,
    # error_tail, change_summary, exit_reason, source, started_at, ended_at).
    # Append-or-replace by (loop, iteration); each per-loop list capped so a
    # runaway can't blow the column up. JSONB rather than a side table because
    # the per-task attempt count is small (single digits typical, ~50 max
    # capped) and the column is read together with the rest of Task on the
    # admin drilldown endpoint.
    fix_loop_history: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")))

    # Stores parsed plan output from plan-type tasks (for debugging/admin visibility)
    plan_output: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))

    # Agent Heartbeat mechanism
    last_heartbeat: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Heartbeat-hot fields promoted from Task.context JSONB (audit 2026-04-29 B2 / C3).
    # The heartbeat handler rewrote the whole context blob every 30s/VM — at 200
    # VMs that's 6.6 full-blob TOAST page rewrites per second. Promoting these
    # two hot subkeys to dedicated columns lets Postgres do in-place column
    # updates without TOAST churn. Audit-trail-ish ring buffers
    # (metrics_history, _tool_trace) stay in context but only persist every
    # 5th heartbeat (handler-side modulo).
    last_metrics_json: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    last_tool_use_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Heartbeat audit-trail gate counter (R3-W1 / Round 3 C1, alembic 0066).
    # Round 2 F2 stored this in ``task.context['_heartbeat_count']`` to gate
    # the per-5th-cycle JSONB writes (metrics_history / _tool_trace). But
    # bumping a JSONB subkey re-serializes the whole blob, defeating B2's
    # TOAST savings. Promoted to a dedicated column so the heartbeat path
    # can ``Task.heartbeat_count = Task.heartbeat_count + 1`` in SQL
    # without dirtying ``context``.
    heartbeat_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    priority: int = Field(default=50, sa_column=Column(Integer, nullable=False, server_default=text("50")))

    attempt_count: int = Field(default=0)
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    started_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Auto-populated on INSERT (default_factory + server_default) and UPDATE
    # (onupdate). Consumed by reaper.py's EVALUATING stale sweep and
    # rollup_management.py age calculations so "last touched" can be
    # distinguished from "first started". TIMESTAMPTZ as of migration 0061;
    # ``server_default=now()`` was already in place per migration 0033.
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(
            NaiveUTCDateTime,
            nullable=False,
            server_default=func.now(),
            onupdate=_utcnow,
        ),
    )
    archived: bool = Field(default=False)

    # VM lifecycle timing for cost tracking
    vm_provisioned_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_started_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_stopped_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_spot: bool = Field(default=False)

    # Lifecycle flags promoted from JSONB context (audit 2026-04-25 W1).
    # Reading these via SELECT FOR UPDATE returns DB-fresh values without
    # the stale-in-memory races that JSONB-based idempotency guards had.
    post_commit_pending: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false")))
    post_commit_failed: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false")))
    requeue_in_flight: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false")))
    infra_wait_until: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    retry_after_until: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    previous_error_fingerprint: Optional[str] = Field(default=None)
    auto_remediation: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false")))
    strategic_cycle_id: Optional[str] = Field(default=None, max_length=36)
    spawn_failed: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false")))
    last_remediation_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    last_token_refresh_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    provider: Optional[str] = Field(default=None)
    account_id: Optional[str] = Field(default=None)
    model: Optional[str] = Field(default=None)
    estimated_cost_usd: Optional[float] = Field(default=None, sa_column=Column(Float, nullable=True))

    # Optimistic-concurrency version (audit 2026-04-29 Phase C4 / finding M-C4):
    # Round 2 audit (C4 + C5) dropped the ``__mapper_args__["version_id_col"]``
    # enforcement: bulk Core UPDATEs (``update(Task).where(...).values(...)``)
    # bypass the mapper entirely and there were zero ``StaleDataError`` handlers
    # in the codebase, so every ORM commit could surface a 500 on concurrent
    # stale writes. The column is retained as a forensic counter — bulk archive
    # paths bump it explicitly via ``.values(version=Task.version + 1)`` in F4.
    #
    # Round 3 H10 dropped the version-axis check from
    # ``_release_parent_lock`` / ``_relock_parent`` because the
    # ``onupdate=_utcnow`` on ``updated_at`` already advances on every
    # mutation. The version axis remains as a forensic counter only.
    #
    # Default 1 (not 0) so freshly-constructed rows match what the server
    # default emits on raw INSERTs that bypass the ORM (e.g. backfill SQL).
    version: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )

    @property
    def first_payload(self) -> Dict[str, Any]:
        """First payload dict, defaulting to empty dict if none exist.

        When the caller projected `payloads[0]` SQL-side (admin_digest.py does
        this to avoid loading the full JSONB array for 5000-row scans), the
        value is stashed on `_projected_first_payload` and returned directly —
        skipping the deferred-column round-trip that `self.payloads` would
        trigger when `load_only()` excluded it.
        """
        if hasattr(self, "_projected_first_payload"):
            return self._projected_first_payload or {}
        return (self.payloads or [{}])[0]

    __table_args__ = (
        # Note: ``ix_task_status_priority`` (status, priority) and
        # ``ix_task_project_id_created_at`` (project_id, created_at) were
        # dropped in alembic 0060 — both fully shadowed by the broader
        # composites below via left-prefix matching (audit 2026-04-29 / H-S1,
        # H-S2). Don't reintroduce without confirming a query needs only the
        # narrower prefix.
        Index("ix_task_status_priority_created", "status", "priority", "created_at"),
        Index("ix_task_project_status_created", "project_id", "status", "created_at"),
        Index(
            "ux_task_active_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text(
                "idempotency_key IS NOT NULL AND status IN "
                "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING')"
            ),
            sqlite_where=text(
                "idempotency_key IS NOT NULL AND status IN "
                "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING')"
            ),
        ),
        Index("ix_task_context_gin", "context", postgresql_using="gin"),
        Index("ix_task_depends_on_gin", "depends_on", postgresql_using="gin"),
        Index("ix_task_provider_created_at", "provider", "created_at"),
        Index("ix_task_account_id_created_at", "account_id", "created_at"),
        # Partial expression index on the leading payload's pr_number so the
        # review-pipeline per-PR iteration-cap query (review_pipeline.py) can
        # filter in SQL rather than loading every terminal Task row in the
        # project and filtering in Python. See alembic 0057 / audit C2.
        # The metadata Index registers a column reference (``payloads``) for
        # the test_model_indexes_covered_by_migrations invariant; the alembic
        # 0057 migration creates the actual expression index on
        # ``(payloads -> 0 ->> 'pr_number')`` via raw SQL on Postgres.
        Index(
            "ix_task_payloads_pr_number",
            "payloads",
            postgresql_where=text("archived = false"),
            sqlite_where=text("archived = false"),
        ),
        # Composite indexes for reaper + claim hot paths (audit 2026-04-29
        # Phase E1 / findings M-P2, M-P3). Both partial on Postgres so the
        # index pages stay small (terminal/top-level rows excluded). The
        # alembic 0064 migration creates them CONCURRENTLY on Postgres;
        # the metadata Index entries here register the names + column
        # references for the test_model_indexes_covered_by_migrations
        # invariant.
        Index(
            "ix_task_status_last_heartbeat",
            "status",
            "last_heartbeat",
            postgresql_where=text("status IN ('IN_PROGRESS', 'PROVISIONING')"),
        ),
        Index(
            "ix_task_parent_task_id_status",
            "parent_task_id",
            "status",
            postgresql_where=text("parent_task_id IS NOT NULL"),
        ),
        # Partial expression index on the leading payload's ``type`` so the
        # orchestrator pipeline budget query (every 5s) can filter active
        # tasks by type without SeqScanning every PROVISIONING/IN_PROGRESS
        # row. Postgres-only via alembic 0067 raw SQL on
        # ``(payloads -> 0 ->> 'type')`` WHERE status IN ('PROVISIONING',
        # 'IN_PROGRESS'). Metadata Index entry registers the name + a
        # column reference (``payloads``) for the
        # test_model_indexes_covered_by_migrations invariant. See R4-W2 / H5.
        Index(
            "ix_task_payloads_type_active",
            "payloads",
            postgresql_where=text("status IN ('PROVISIONING', 'IN_PROGRESS')"),
        ),
        # Partial index on retry_after_until so the reaper's stale
        # retry_after sweep (reaper.py: status=QUEUED AND
        # retry_after_until IS NOT NULL) doesn't degrade to linear scan
        # of the QUEUED queue at 1k users. Postgres-only via alembic 0069
        # CONCURRENTLY DDL; the metadata Index registers the name +
        # column for ``test_model_indexes_covered_by_migrations``.
        # See R5-W2-C.
        Index(
            "ix_task_retry_after_active",
            "retry_after_until",
            postgresql_where=text(
                "status = 'QUEUED' AND retry_after_until IS NOT NULL"
            ),
            sqlite_where=text(
                "status = 'QUEUED' AND retry_after_until IS NOT NULL"
            ),
        ),
        # Defense-in-depth size cap on payloads JSONB (audit 2026-04-29 D4 /
        # finding M-S5). The Python layer's ``pack_under_budget`` already caps
        # strategic-spawn payloads at 120 KB, but a future code path could
        # insert a runaway. The 200 KB CHECK constraint chk_task_payloads_size
        # is enforced server-side by alembic 0063 (Postgres-only). Not declared
        # here in __table_args__ because the constraint expression
        # ``octet_length(payloads::text) < 200000`` is Postgres-specific syntax
        # that breaks SQLite-fixture tests using metadata.create_all.
        # See alembic 0063 / memory ``reference_strategic_payload_bloat.md``.
    )

    # Optimistic concurrency: ``version_id_col`` mapper arg was dropped in
    # round 2 audit (C4 + C5) — see the comment on the ``version`` Field
    # above. Bulk Core UPDATEs bypassed it and no ``StaleDataError``
    # handlers existed, so ORM commits leaked 500s on concurrent stale
    # writes. The column itself is retained as a forensic counter; bulk
    # archive paths bump it explicitly in F4.

    project: Project = Relationship(back_populates="tasks")
    artifacts: List["Artifact"] = Relationship(
        back_populates="task",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    history: List["TaskHistory"] = Relationship(
        back_populates="task",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

class TaskHistory(SQLModel, table=True):
    """
    Audit trail for task modifications, splits, and spawns.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="task.id", index=True)

    timestamp: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    action: str # e.g., "STATUS_CHANGE", "SPLIT", "SPAWNED_CHILD", "MODIFIED_PAYLOAD"
    details: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))

    task: Task = Relationship(back_populates="history")

# Auto-append to status_log whenever latest_status_message is set
@event.listens_for(Task.latest_status_message, "set")
def _on_status_message_set(target: Task, value, oldvalue, initiator):
    if value and value != oldvalue:
        entry = {"t": _utcnow().isoformat(), "msg": str(value)[:500]}
        target.status_log = ((target.status_log or []) + [entry])[-90:]

# Callbacks invoked when a task reaches a terminal state.
# main.py registers _terminated_task_ids.add here to revoke agent JWTs.
_on_task_terminal_callbacks: list = []

# Callbacks invoked when a task is revived from a terminal state OR set to
# any non-terminal status. Used to clear any in-memory deny-lists populated
# by terminal callbacks (notably ``_terminated_task_ids`` in agent.py).
#
# Production 2026-05-05: task 3d4bc05a was stuck in a claim 401 loop:
#   1. A prior in-flight transition set status=FAILED (in memory) — fired
#      terminal callback → ``_terminated_task_ids[task_id] = now``.
#   2. The transaction was rolled back (e.g. agent_status playbook block
#      poisoned the session), reverting the DB but leaving the cache
#      populated.
#   3. The reaper later requeued the task to QUEUED, the orchestrator
#      committed PROVISIONING, but the cache stayed set — so every new
#      VM's ``/api/agent/claim`` hit ``_terminated_task_ids`` and got 401
#      "Task is no longer active", looping forever until the orchestrator
#      revision restarted (wiping the in-memory cache).
#
# Firing revive callbacks on every non-terminal status set restores the
# cache invariant so the agent can claim cleanly.
_on_task_revive_callbacks: list = []

@event.listens_for(Task.status, "set")
def _on_status_set(target: Task, value, oldvalue, initiator):
    if value and isinstance(oldvalue, str) and value != oldvalue:
        entry = {"t": _utcnow().isoformat(), "msg": f"Status changed: {oldvalue} → {value}"}
        target.status_log = ((target.status_log or []) + [entry])[-90:]
        task_id = str(target.id)
        # Notify JWT revocation when task reaches terminal state
        if value in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            for cb in _on_task_terminal_callbacks:
                try:
                    cb(task_id)
                except Exception as e:
                    logger.error(f"[MODELS] Terminal callback failed for task {task_id}: {e}", exc_info=True)
        else:
            # Non-terminal status set (QUEUED, PROVISIONING, IN_PROGRESS,
            # EVALUATING). Fire revive callbacks so any deny-list entries
            # populated by an earlier terminal transition are cleared.
            for cb in _on_task_revive_callbacks:
                try:
                    cb(task_id)
                except Exception as e:
                    logger.error(f"[MODELS] Revive callback failed for task {task_id}: {e}", exc_info=True)


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"

class Alert(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    severity: AlertSeverity = Field(default=AlertSeverity.ERROR, sa_column=Column(String, default=AlertSeverity.ERROR))
    title: str
    message: str
    # Audit 2026-04-29 (M-S3): FK to task.id with ondelete=SET NULL added in
    # migration 0056. Alert messages survive parent-task GC for the admin UI.
    task_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(ForeignKey("task.id", ondelete="SET NULL"), nullable=True),
    )
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    dismissed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    __table_args__ = (
        Index("ix_alert_task_id_dismissed", "task_id", "dismissed_at"),
    )


class StrategicCycle(SQLModel, table=True):
    """Strategic-research cycle record.

    Audit 2026-04-25 W2 (project audit_pipeline_2026_04_25): cycles previously
    lived in a 10-entry sliding JSONB list on ``Project.vision._strategic_cycle_history``.
    The cap silently dropped old entries — including ones stuck
    ``outcome="in_progress"`` from phase-1 crashes that never reached the
    completion handler — so the strategic agent had no diagnostic signal that
    cycles were failing.

    The cycle has two phases:
      - ``phase1`` (design_doc_update): the architecture-analysis task
      - ``phase2`` (strategic_review): the vision/direction-update task
    Phase 2 spawns synchronously on phase 1 completion (see
    ``task_completion._spawn_strategic_review``); the lifecycle hook in
    ``task_lifecycle`` is a backstop that still updates the cycle row when
    the synchronous chain crashes mid-flight.
    """
    id: str = Field(primary_key=True, max_length=36)  # cycle_id (UUID hex)
    project_id: uuid.UUID = Field(foreign_key="project.id")

    # outcome enum-ish: in_progress | phase1_running | phase1_done |
    # phase2_running | completed | failed_phase1 | failed_phase2 |
    # stalled_phase1 | stalled_phase2 | failed_phase2_spawn
    outcome: str = Field(default="in_progress", sa_column=Column(String(32), nullable=False, server_default="in_progress"))

    # Phase tracking. *_started_at = task creation time;
    # *_completed_at = terminal status.
    phase1_task_id: Optional[uuid.UUID] = Field(default=None, foreign_key="task.id")
    phase1_started_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    phase1_completed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    phase2_task_id: Optional[uuid.UUID] = Field(default=None, foreign_key="task.id")
    phase2_started_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    phase2_completed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Outcome accounting (filled on phase 2 completion).
    directions_created: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default=text("0")))
    directions_retired: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default=text("0")))
    tasks_spawned: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default=text("0")))
    vision_updated: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default=text("false")))

    # Free-form text fields surfaced to the UI / next cycle's context.
    cycle_reasoning: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    summary_text: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    failure_reason: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))

    # Per-direction maps, baseline_vision snapshot for 3-way merge, etc.
    # No server_default — Python default_factory keeps it consistent across
    # SQLite (test) and Postgres (prod) without per-dialect cast syntax.
    cycle_metadata: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, default=dict))

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )


class OrchestratorErrorAck(SQLModel, table=True):
    """Persistent acknowledgement of an orchestrator error bucket.

    key is "exception_class|source" for a specific bucket, or "*" for a
    global ack covering every bucket whose last_seen_iso <= acknowledged_at.
    """
    key: str = Field(primary_key=True, max_length=255)
    acknowledged_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    acknowledged_by: str = Field(default="")


class MessageType(str, Enum):
    QUESTION = "QUESTION"
    FEEDBACK = "FEEDBACK"
    BUG_REPORT = "BUG_REPORT"
    BLOCKER = "BLOCKER"
    NOTE = "NOTE"

class ProjectMessage(SQLModel, table=True):
    """Async message board between users and the strategic agent."""
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    message_type: MessageType = Field(default=MessageType.NOTE, sa_column=Column(String, default=MessageType.NOTE))
    # Allowed values: 'user' | 'agent' | 'strategic' | 'system'. Enforced
    # server-side by ``chk_projectmessage_from_type`` (alembic 0065 /
    # audit L-S1, Postgres-only).
    from_type: str = Field(default="user")
    from_task_id: Optional[uuid.UUID] = Field(default=None, foreign_key="task.id")
    content: str = Field(default="")
    context: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=True, default={}))
    status: str = Field(default="open")  # "open" | "resolved"
    resolution: Optional[str] = None
    resolved_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        Index("ix_projectmessage_project_status", "project_id", "status"),
    )


class DeploymentStatus(str, Enum):
    MONITORING = "MONITORING"    # New deploy, within observation window
    STABLE = "STABLE"            # Observation window passed, no issues
    ROLLED_BACK = "ROLLED_BACK"  # Reverted to last known good
    PAUSED = "PAUSED"            # External dependency down, dispatch halted
    HALTED = "HALTED"            # Multiple consecutive rollbacks, manual intervention needed

class DeploymentState(SQLModel, table=True):
    """
    Singleton row tracking the current deployment health.
    Used for rollback detection, monitoring windows, and progressive trust.
    """
    id: int = Field(default=1, primary_key=True)
    current_revision: str = Field(default="")       # Cloud Run K_REVISION
    current_sha: str = Field(default="")             # git commit SHA
    deployed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    status: str = Field(default=DeploymentStatus.STABLE, sa_column=Column(String, default=DeploymentStatus.STABLE))
    last_known_good_revision: str = Field(default="")
    last_known_good_sha: str = Field(default="")
    rolled_back_from_sha: Optional[str] = Field(default=None)
    rollback_reason: Optional[str] = Field(default=None)
    rollback_count: int = Field(default=0)           # consecutive rollbacks (reset on STABLE)
    pause_reason: Optional[str] = Field(default=None)
    paused_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Cumulative pause-time, split by origin. Timers consult the sum via
    # app.pause_clock to subtract pause windows from their elapsed
    # calculations. Split for observability (self-inflicted vs external-dep
    # downtime); timer math always uses the sum.
    total_manual_paused_seconds: float = Field(default=0.0)
    total_auto_paused_seconds: float = Field(default=0.0)
    # "manual" or "auto" while paused; None while running. Routes the
    # in-flight window to the correct counter at resume.
    paused_kind: Optional[str] = Field(default=None)
    # Pre-deploy saved max_concurrent_vms for PAUSE mode restore
    pre_pause_max_vms: Optional[int] = Field(default=None)
    # Persisted runtime config — restored on startup so settings survive restarts
    runtime_max_concurrent_vms: Optional[int] = Field(default=None)
    runtime_max_retries: Optional[int] = Field(default=None)
    runtime_max_autonomous_tasks_per_hour: Optional[int] = Field(default=None)
    runtime_max_global_autonomous_tasks_per_hour: Optional[int] = Field(default=None)
    runtime_autonomous_research_interval_minutes: Optional[int] = Field(default=None)
    runtime_autonomous_direction_cooldown_minutes: Optional[int] = Field(default=None)
    runtime_autonomous_research_model: Optional[str] = Field(default=None)
    runtime_autonomous_research_effort: Optional[str] = Field(default=None)
    # Persisted circuit breaker states — survives restarts/deploys
    circuit_breaker_states: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Persisted zone health/cooldown — prevents retry storms after restart
    zone_health_state: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Persisted dict/list runtime config (task_type_models, agent_prompt_suffixes, etc.)
    runtime_config_json: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Persisted anomaly detection state — occurrence counts and active adjustments survive restarts
    persistent_anomaly_state: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Persisted direction failure streaks — holds survive restarts silently
    direction_streak_state: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Persisted pipeline events ring buffer — alert windows survive restarts
    pipeline_events_state: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Timestamp when HALTED state was entered — used for auto-recovery timer
    halted_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Optimistic-concurrency version (audit 2026-04-25 W1 / F4): bumped on
    # every write so concurrent mutations from different code paths can
    # detect lost-write hazards without holding the row lock for the whole
    # post-commit cycle.
    version: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default=text("0")))


class MaxPlanAccount(SQLModel, table=True):
    """
    Persisted LLM account record used by the current Anthropic pool and future
    multi-provider account selection. In v1, Anthropic OAuth remains the only
    runnable path; other provider/auth combinations are stored for future use.
    Active task counts are tracked in-memory only.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    name: str = Field(max_length=255)
    provider: LLMProviderName = Field(
        default=LLMProviderName.ANTHROPIC,
        sa_column=Column(
            SAEnum(LLMProviderName, name="llmprovider", values_callable=lambda obj: [e.value for e in obj]),
            nullable=False,
            default=LLMProviderName.ANTHROPIC,
        ),
    )
    auth_mode: LLMAuthMode = Field(
        default=LLMAuthMode.OAUTH,
        sa_column=Column(
            SAEnum(LLMAuthMode, name="llmauthmode", values_callable=lambda obj: [e.value for e in obj]),
            nullable=False,
            default=LLMAuthMode.OAUTH,
        ),
    )
    oauth_token: Optional[str] = Field(default=None)  # Secret Manager resource name (or raw token for backward compat)
    claude_config_json: Optional[str] = Field(default=None)  # ~/.claude.json content for onboarding bypass
    secret_blob_ref: Optional[str] = Field(default=None)
    provider_metadata: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    enabled: bool = Field(default=True)
    total_tasks: int = Field(default=0)
    token_created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )  # When the token was added (for age tracking)
    rate_limit_state: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # Usage caps (null = unlimited).
    daily_task_cap: Optional[int] = Field(default=None)
    max_concurrent_tasks: Optional[int] = Field(default=None)
    auto_disable_after_rate_limits: Optional[int] = Field(default=None)
    daily_task_count: int = Field(default=0)
    daily_task_count_reset_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    disabled_reason: Optional[str] = Field(default=None)
    # Set when auto-disable trips (rate-limit threshold). Persisted so
    # recheck_disabled_accounts can re-enable after cooldown across
    # orchestrator restarts. Null for never-disabled or admin-disabled
    # accounts (admin disable doesn't auto-recover).
    disabled_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )


# --- Residual-case reaper thresholds ---
# All are tunable via runtime_config using the same-named lowercase keys
# (e.g. runtime_config.get("residual_stale_close_hours", RESIDUAL_STALE_CLOSE_HOURS)).
# RESIDUAL_STALE_CLOSE_HOURS:     Hours of fingerprint silence before an ACTIVE case is
#   closed organically OR a stuck REMEDIATING case is auto-resolved to RESOLVED.
# RESIDUAL_RECENT_HIT_DEFER_HOURS: If a fingerprint's last_seen_at is within this window,
#   defer the ACTIVE → CLOSED_ORGANICALLY close even if the 72 h outer window has elapsed.
# RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD: Consecutive audit deferrals on a single
#   fingerprint before a BLOCKER ProjectMessage is posted (one-shot per streak; resets
#   to 0 when a fix spawns so future audit cycles can re-escalate).
RESIDUAL_STALE_CLOSE_HOURS = 72
RESIDUAL_RECENT_HIT_DEFER_HOURS = 1
RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD = 5


class ResidualCaseStatus(str, Enum):
    # Passive states (existing): set by record_residual_fingerprint + reaper
    ACTIVE = "ACTIVE"
    CLOSED_ORGANICALLY = "CLOSED_ORGANICALLY"
    IGNORED = "IGNORED"
    RESOLVED = "RESOLVED"
    # Lifecycle states (added 0054): set by autonomous_research spawn path +
    # notification_dispatch.post_residual_fix_result_message. Mirror the
    # CI auto-remediation lifecycle (open → remediating → merged|failed →
    # suspended after threshold) using existing ACTIVE as the open state.
    REMEDIATING = "REMEDIATING"
    MERGED = "MERGED"
    FAILED = "FAILED"
    SUSPENDED = "SUSPENDED"


# Backward-compatible alias used by admin_digest.py and autonomous_research.py
ResidualRemediationCaseStatus = ResidualCaseStatus


class ResidualRemediationCase(SQLModel, table=True):
    """Persistent record for a residual CODE_ERROR fingerprint.

    Created or updated whenever record_residual_fingerprint fires; closed by
    the reaper after 72 h of silence on the fingerprint.
    Status IGNORED suppresses auto-remediation spawning for that fingerprint.
    Status SUSPENDED suppresses auto-spawn after consecutive_failure_count
    reaches the escalation threshold; cleared manually via admin endpoint.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: Optional[uuid.UUID] = Field(default=None, foreign_key="project.id", index=True)
    fingerprint: str = Field(index=True, max_length=512)
    status: str = Field(
        default=ResidualCaseStatus.ACTIVE,
        sa_column=Column(String, nullable=False, default=ResidualCaseStatus.ACTIVE),
    )
    first_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    last_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    hit_count: int = Field(default=1)
    sample_message: Optional[str] = Field(default=None)
    # Lifecycle fields (added 0054). All nullable / default-0 / default-[]
    # so existing rows backfilled by the migration's server_default work
    # without a Python-side touch.
    last_spawned_task_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(ForeignKey("task.id", ondelete="SET NULL"), nullable=True),
    )
    last_spawned_pr_number: Optional[int] = Field(default=None)
    last_spawned_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    consecutive_failure_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    suspended_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    sample_messages: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    sample_statuses: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    task_ids: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )
    # Audit-mode deferral fields (added 0056). Nullable / default-0 so existing
    # rows backfilled by migration server_default work without a Python-side touch.
    audit_defer_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    audit_deferred_system_id: Optional[str] = Field(default=None)

    __table_args__ = (
        Index("ix_residual_case_status_last_seen", "status", "last_seen_at"),
        Index(
            "ix_residualremediationcase_project_status_spawned",
            "project_id", "status", "last_spawned_at",
        ),
        UniqueConstraint(
            "project_id", "fingerprint",
            name="ux_residualremediationcase_project_fingerprint",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'IGNORED', 'CLOSED_ORGANICALLY', 'RESOLVED', "
            "'REMEDIATING', 'SUSPENDED', 'MERGED', 'FAILED')",
            name="ck_residualremediationcase_status",
        ),
    )


class Artifact(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="task.id", index=True)

    artifact_type: ArtifactType = Field(sa_column=Column(String, nullable=False))

    # Audit 2026-04-25 A14: partial UNIQUE on (task_id) WHERE artifact_type='PULL_REQUEST'
    # closes the SELECT-then-INSERT race in agent_status.update_agent_status that
    # could create two PR artifacts pointing at the same PR. Other artifact types
    # (GIT_COMMIT, ERROR_LOG, SCREENSHOT, ...) legitimately have multiple rows per
    # task, so the constraint is partial. Migration 0050.
    __table_args__ = (
        Index(
            "ux_artifact_task_pull_request",
            "task_id",
            unique=True,
            postgresql_where=text("artifact_type = 'PULL_REQUEST'"),
        ),
    )

    # Git-based outputs
    commit_hash: Optional[str] = None
    repo_url: Optional[str] = None
    pr_url: Optional[str] = None

    # For visual feedback or logs
    storage_uri: Optional[str] = None
    logs: Optional[str] = None

    iteration: int = Field(default=0)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )

    task: Task = Relationship(back_populates="artifacts")


class SystemMaturity(SQLModel, table=True):
    """Per-system maturity snapshot written by self_audit tasks.

    Keyed on (project_id, system_id) with a unique constraint so upserts are
    well-defined. system_id is a free-form string that matches direction
    system_id values stored in the Project.directions JSONB list — no FK into
    a system catalog is enforced here.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)

    system_id: str = Field(index=True)

    # 0-100 overall health score; validated at ingest, not enforced by a DB check constraint.
    score: int

    # Per-component breakdown, e.g. {test_coverage, friction_density, claim_consistency, failure_mix}
    components: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    # audit | fundamentals | ship | pause — validated at ingest
    recommended_mode: Optional[str] = None

    top_issues: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # Citations / file pointers / metric snapshots supporting the score
    evidence: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # TIMESTAMPTZ + NaiveUTCDateTime (alembic 0052 made these tz-aware;
    # round 3 audit H9 routes them through the type decorator so loads
    # return naive UTC consistent with the rest of the codebase, and
    # tz-aware writes get normalized via process_bind_param).
    last_audited_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    source_task_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(
            GUID(),
            ForeignKey("task.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        UniqueConstraint("project_id", "system_id", name="uq_system_maturity_project_system"),
    )


