from enum import Enum
from typing import Dict, Any, List
from sqlmodel import SQLModel, Field, Relationship
from sqlmodel.sql.sqltypes import GUID
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    String,
    Integer,
    Float,
    Index,
    Text,
    UniqueConstraint,
    ForeignKey,
    text,
    event,
    func,
    Enum as SAEnum,
)
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
    RELEASE_AUDIT_REPORT = "RELEASE_AUDIT_REPORT"
    MEMO = "MEMO"


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
    firebase_uid: str | None = Field(default=None, index=True)
    # Allowed values: 'admin' | 'user'. Enforced server-side by
    # ``chk_alloweduser_role`` (alembic 0065 / audit L-S1, Postgres-only).
    role: str = Field(default="user")
    display_name: str | None = None
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5):
    # default_factory is retained as an additive Python-side guard so ORM
    # callers don't see ``None`` between construction and commit; the
    # server_default protects raw-SQL INSERT paths that bypass the ORM.
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    last_login_at: datetime | None = Field(
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
    github_installation_id: str | None = None
    github_account_name: str | None = None

    # GitHub Fine-Grained Personal Access Token (Alternative to App Installation)
    github_pat: str | None = None

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (UniqueConstraint("user_id", name="uq_userintegration_user_id"),)


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
    status: ProjectStatus = Field(
        default=ProjectStatus.DRAFT,
        sa_column=Column(String, default=ProjectStatus.DRAFT),
    )
    github_owner: str | None = None
    github_repo: str | None = None
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
    directions: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSONB, default=[])
    )
    # Strategic product vision maintained by the strategic research agent.
    # Legacy: being replaced by ``problem_model`` (see below). During migration
    # we render vision from the problem model; once all consumers migrate, this
    # column becomes a cache or is removed.
    vision: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    # Structured problem model — the strategic layer's durable understanding.
    # Replaces vision + design_doc/notes/strategic-context as the source of
    # truth for strategic state. Shape defined in app.problem_model.ProblemModel.
    problem_model: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
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
    running_update: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    running_update_acknowledged_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Living architectural description maintained by design doc update agent
    design_doc: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    # Toggle autonomous research/task generation
    autonomous_enabled: bool = Field(default=True)
    # Self-repo flag: enables rollback monitoring, extended windows, infra soft gates
    # Set True when github_owner/github_repo matches Blueprint's own repo
    is_self_repo: bool = Field(default=False)
    # Build configuration: directories, commands, git identity for the agent
    build_config: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Project-level CI auto-remediation outcomes for fixes that could not be
    # linked to a direction (novel failures whose fingerprint matched no
    # direction). Shape: {"attempts": int, "last_result": str, "last_at": iso,
    # "history": [{task_id, result, timestamp, fingerprint, failure_class}]}.
    remediation_tracking: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Regression-class trip markers used to persist first-tripped timestamps
    # across dispatcher ticks when a signal has no durable historical record.
    regression_signal_state: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    # Per-category self-review catch-rate targets set by the operator.
    # Shape: {category_id: float} e.g. {'security': 0.80, 'regression_risk': 0.75}.
    # Absent key = no target configured for that category (default: no alerting).
    sr_catch_rate_targets: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    def append_routine_digest(self, event_type: str, summary: str) -> None:
        """Append a one-line digest entry for a routine event to running_update.

        Canonical home for this logic — callers (notification_dispatch, admin_notifications,
        strategic_completion) mutate running_update through this method so the format stays
        consistent. Does NOT commit — caller is responsible for session.add(project).
        """
        now_str = _utcnow().strftime("%Y-%m-%d %H:%M")
        line = f"- [{now_str}] **{event_type}**: {summary[:200]}"
        current = (self.running_update or "").rstrip()
        self.running_update = f"{current}\n{line}" if current else line

    tasks: List["Task"] = Relationship(
        back_populates="project",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
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


class MessagesLastSeen(SQLModel, table=True):
    """Per-operator, per-project last-seen timestamp for the messages panel divider.

    Enables cross-device persistence of the since-last-visit divider position.
    localStorage remains the offline fallback; this table is the authoritative
    source when the backend is reachable.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, max_length=128)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    last_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", name="uq_messageslastseen_user_project"
        ),
    )


class MessagesNoisePrefs(SQLModel, table=True):
    """Per-operator, per-project messages-panel noise-control preferences.

    Persists the operator's per-event-type visibility choices and saved filter
    views server-side so they follow the operator across devices. localStorage
    remains the offline fallback; this table is the authoritative source when
    the backend is reachable.

    prefs shape (stored as JSONB, validated by the admin_notifications router):
      {
        "event_type_visibility": {<event_type>: "show"|"mute"|"routine"},
        "saved_filter_views": [{"name": str, "filters": {...}}, ...]
      }
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, max_length=128)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    prefs: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", name="uq_messagesnoiseprefs_user_project"
        ),
    )


class MessagesSavedViews(SQLModel, table=True):
    """Per-operator, per-project saved views for the messages panel.

    Persists named filter/sort combinations (saved views) server-side so they
    follow the operator across devices. localStorage remains the offline fallback;
    this table is the authoritative source when the backend is reachable.

    saved_views shape (stored as JSONB, validated by the admin_notifications router):
      [{"name": str, "query": str, "event_prefs": {...}, "sort_mode": str,
        "show_resolved": bool, "show_routine": bool, "only_new": bool}, ...]
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, max_length=128)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    saved_views: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "project_id", name="uq_messagessavedviews_user_project"
        ),
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
    parent_task_id: uuid.UUID | None = Field(
        default=None, foreign_key="task.id", index=True
    )
    latest_status_message: str | None = None
    status_log: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # Task provenance. Allowed canonical values:
    #   'USER' | 'ORCHESTRATOR' | 'STRATEGIC_AGENT' |
    #   'SYSTEM_AUTO_REMEDIATION' | 'SYSTEM_AUTO_REVIEW' |
    #   'SYSTEM_ISSUE_IMPLEMENTATION'
    # Test fixtures additionally use 'TEST' / 'test-user' / 'user' / 'user1'
    # — these are accepted by the CHECK constraint but should not be
    # introduced in production code paths. Enforced server-side by
    # ``chk_task_created_by`` (alembic 0065 / audit L-S1, Postgres-only).
    created_by: str = Field(default="USER", index=True)
    launched_by_user_id: str | None = Field(
        default=None
    )  # Firebase UID of the user who launched this task (for launcher-pays billing)
    idempotency_key: str | None = Field(
        default=None, sa_column=Column(String(255), nullable=True)
    )
    requires_approval: bool = Field(default=False)

    status: TaskStatus = Field(
        default=TaskStatus.QUEUED, sa_column=Column(String, default=TaskStatus.QUEUED)
    )

    # Push Model: Orchestrator assigns this task to a specific VM
    vm_instance_id: str | None = Field(default=None, index=True)

    # Task dependency DAG: list of task UUIDs that must be COMPLETED+merged before dispatch
    depends_on: List[str] = Field(
        default_factory=list, sa_column=Column(JSONB, default=[])
    )

    # Flexible Payloads: A list of structured dictionaries defining the work.
    # E.g., [{"interface": "ui_component_v1", "data": {...}}]
    payloads: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )

    # Shared memory/context for the agent during execution
    context: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

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
    fix_loop_history: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )

    # Stores parsed plan output from plan-type tasks (for debugging/admin visibility)
    plan_output: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    # Agent Heartbeat mechanism
    last_heartbeat: datetime | None = Field(
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
    last_metrics_json: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    last_tool_use_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Stable 12-hex prompt digest for the assembled guidance that drove this task.
    prompt_fingerprint: str | None = Field(default=None, max_length=12)

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

    priority: int = Field(
        default=50, sa_column=Column(Integer, nullable=False, server_default=text("50"))
    )

    attempt_count: int = Field(default=0)
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    started_at: datetime | None = Field(
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
    # Write-once timestamp recorded at initial dispatch (before GCE create call).
    # Used as the anchor for vm_cold_start_ms so the measurement covers the full
    # dispatch→agent-ready interval including GCE boot latency.  Never re-stamped.
    vm_dispatched_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_provisioned_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_stopped_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    vm_spot: bool = Field(default=False)

    # Efficiency-direction (d-91d8dede) foundational instrumentation.
    # vm_cold_start_ms = (first agent heartbeat - vm_provisioned_at) in ms.
    # Recorded once per VM lifecycle and guarded by IS NULL so a re-provision
    # restamp of vm_provisioned_at (orchestrator.py line ~3240) does not
    # overwrite the genuine cold-start measurement.
    vm_cold_start_ms: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    # vm_warm_start is True when the VM was reused from a warm pool / cached
    # image rather than freshly provisioned. There is no warm pool today, so
    # all provisions write False — the column is foundational scaffolding so
    # that vm_reuse_rate has a real signal once warm pooling lands.
    vm_warm_start: bool | None = Field(
        default=False,
        sa_column=Column(Boolean, nullable=True, server_default=text("false")),
    )
    # Cumulative per-task LLM telemetry posted by the agent on every heartbeat.
    # Shape: {"llm_call_count": int, "total_input_tokens": int,
    # "total_output_tokens": int, "last_updated_at": iso8601}.
    # Backwards-compatible — older agents that don't ship these fields leave
    # this column NULL.
    api_call_summary: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    # Lifecycle flags promoted from JSONB context (audit 2026-04-25 W1).
    # Reading these via SELECT FOR UPDATE returns DB-fresh values without
    # the stale-in-memory races that JSONB-based idempotency guards had.
    post_commit_pending: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    post_commit_failed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    requeue_in_flight: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    infra_wait_until: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    retry_after_until: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    previous_error_fingerprint: str | None = Field(default=None)
    auto_remediation: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    strategic_cycle_id: str | None = Field(default=None, max_length=36)
    spawn_failed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    last_remediation_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    last_token_refresh_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    provider: str | None = Field(default=None)
    account_id: str | None = Field(default=None)
    model: str | None = Field(default=None)
    estimated_cost_usd: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )

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
            sqlite_where=text("status = 'QUEUED' AND retry_after_until IS NOT NULL"),
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
        back_populates="task", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    history: List["TaskHistory"] = Relationship(
        back_populates="task", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
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
    action: str  # e.g., "STATUS_CHANGE", "SPLIT", "SPAWNED_CHILD", "MODIFIED_PAYLOAD"
    details: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))

    task: Task = Relationship(back_populates="history")


# Auto-append to status_log whenever latest_status_message is set
@event.listens_for(Task.latest_status_message, "set")
def _on_status_message_set(target: Task, value, oldvalue, initiator):
    if value and value != oldvalue:
        entry = {"t": _utcnow().isoformat(), "msg": str(value)[:500]}
        try:
            target.status_log = ((target.status_log or []) + [entry])[-90:]
        except Exception as e:  # noqa: BLE001
            # Reading target.status_log fires a sync lazy-load when the
            # attribute is expired (most common cause: the session was
            # rolled back by an upstream DB error, expiring every loaded
            # instance). Under an async session that raises
            # MissingGreenlet; if the underlying asyncpg connection is
            # also dead it raises InterfaceError. Either way the
            # status_log audit append is best-effort — the
            # latest_status_message SET itself still persists via the
            # outer assignment, so we swallow and skip the append rather
            # than crashing the caller's whole transaction.
            # Production 2026-05-08: orchestrator.py:4743 cascade.
            logger.debug(
                "[MODELS] _on_status_message_set: skipped status_log append (%s)",
                e,
            )


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
        entry = {
            "t": _utcnow().isoformat(),
            "msg": f"Status changed: {oldvalue} → {value}",
        }
        try:
            target.status_log = ((target.status_log or []) + [entry])[-90:]
        except Exception as e:  # noqa: BLE001
            # Best-effort audit append — see _on_status_message_set for
            # the expired-attribute / dead-connection rationale.
            logger.debug(
                "[MODELS] _on_status_set: skipped status_log append (%s)",
                e,
            )
        task_id = str(target.id)
        # Notify JWT revocation when task reaches terminal state
        if value in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            for cb in _on_task_terminal_callbacks:
                try:
                    cb(task_id)
                except Exception as e:  # noqa: PERF203
                    logger.error(
                        f"[MODELS] Terminal callback failed for task {task_id}: {e}",
                        exc_info=True,
                    )
        else:
            # Non-terminal status set (QUEUED, PROVISIONING, IN_PROGRESS,
            # EVALUATING). Fire revive callbacks so any deny-list entries
            # populated by an earlier terminal transition are cleared.
            for cb in _on_task_revive_callbacks:
                try:
                    cb(task_id)
                except Exception as e:  # noqa: PERF203
                    logger.error(
                        f"[MODELS] Revive callback failed for task {task_id}: {e}",
                        exc_info=True,
                    )


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Alert(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    severity: AlertSeverity = Field(
        default=AlertSeverity.ERROR,
        sa_column=Column(String, default=AlertSeverity.ERROR),
    )
    title: str
    message: str
    # Audit 2026-04-29 (M-S3): FK to task.id with ondelete=SET NULL added in
    # migration 0056. Alert messages survive parent-task GC for the admin UI.
    task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("task.id", ondelete="SET NULL"), nullable=True),
    )
    # TIMESTAMPTZ + server_default (migration 0061 / audit H-S4 + H-S5).
    # NaiveUTCDateTime strips tz on read (round 2 audit / C3 + H1).
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )
    dismissed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    __table_args__ = (
        Index("ix_alert_task_id_dismissed", "task_id", "dismissed_at"),
        Index("ix_alert_project_id_dismissed", "project_id", "dismissed_at"),
    )


# Keys used in StrategicCycle.cycle_metadata for per-cycle memo-pathway tracking.
# Stored as plain integers so operators and digest endpoints can read them without
# knowing the exact key strings.
CYCLE_META_MEMOS_AVAILABLE = "memos_available"
CYCLE_META_MEMOS_CITED = "memos_cited"


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
    outcome: str = Field(
        default="in_progress",
        sa_column=Column(String(32), nullable=False, server_default="in_progress"),
    )

    # Phase tracking. *_started_at = task creation time;
    # *_completed_at = terminal status.
    phase1_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id")
    phase1_started_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    phase1_completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    phase2_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id")
    phase2_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    phase2_completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Outcome accounting (filled on phase 2 completion).
    directions_created: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    directions_retired: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    tasks_spawned: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )
    vision_updated: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )

    # Free-form text fields surfaced to the UI / next cycle's context.
    cycle_reasoning: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    summary_text: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    failure_reason: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Per-direction maps, baseline_vision snapshot for 3-way merge, etc.
    # No server_default — Python default_factory keeps it consistent across
    # SQLite (test) and Postgres (prod) without per-dialect cast syntax.
    cycle_metadata: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, default=dict)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    @property
    def completed_at(self) -> datetime | None:
        """Effective cycle completion time: phase2 if available, else phase1, else updated_at."""
        return self.phase2_completed_at or self.phase1_completed_at or self.updated_at

    def cycle_ref(self) -> Dict[str, Any]:
        """Compact dict for embedding in stored results and API responses."""
        return {
            "id": self.id,
            "started_at": self.phase1_started_at.isoformat()
            if self.phase1_started_at
            else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
        }

    def memo_stats(self) -> Dict[str, int]:
        """Return per-cycle memo counts from cycle_metadata.

        Keys match CYCLE_META_MEMOS_AVAILABLE / CYCLE_META_MEMOS_CITED constants.
        Returns zeros when not yet recorded (cycle in progress or pre-feature rows).
        """
        meta = self.cycle_metadata or {}
        return {
            CYCLE_META_MEMOS_AVAILABLE: int(meta.get(CYCLE_META_MEMOS_AVAILABLE) or 0),
            CYCLE_META_MEMOS_CITED: int(meta.get(CYCLE_META_MEMOS_CITED) or 0),
        }


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
    message_type: MessageType = Field(
        default=MessageType.NOTE, sa_column=Column(String, default=MessageType.NOTE)
    )
    # Allowed values: 'user' | 'agent' | 'strategic' | 'system'. Enforced
    # server-side by ``chk_projectmessage_from_type`` (alembic 0065 /
    # audit L-S1, Postgres-only).
    from_type: str = Field(default="user")
    from_task_id: uuid.UUID | None = Field(default=None, foreign_key="task.id")
    content: str = Field(default="")
    context: Dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=True, default={})
    )
    status: str = Field(default="open")  # "open" | "resolved"
    resolution: str | None = None
    resolved_at: datetime | None = Field(
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
    MONITORING = "MONITORING"  # New deploy, within observation window
    STABLE = "STABLE"  # Observation window passed, no issues
    ROLLED_BACK = "ROLLED_BACK"  # Reverted to last known good
    PAUSED = "PAUSED"  # External dependency down, dispatch halted
    HALTED = "HALTED"  # Multiple consecutive rollbacks, manual intervention needed


class DeploymentState(SQLModel, table=True):
    """
    Singleton row tracking the current deployment health.
    Used for rollback detection, monitoring windows, and progressive trust.
    """

    id: int = Field(default=1, primary_key=True)
    current_revision: str = Field(default="")  # Cloud Run K_REVISION
    current_sha: str = Field(default="")  # git commit SHA
    deployed_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    status: str = Field(
        default=DeploymentStatus.STABLE,
        sa_column=Column(String, default=DeploymentStatus.STABLE),
    )
    last_known_good_revision: str = Field(default="")
    last_known_good_sha: str = Field(default="")
    rolled_back_from_sha: str | None = Field(default=None)
    rollback_reason: str | None = Field(default=None)
    rollback_count: int = Field(default=0)  # consecutive rollbacks (reset on STABLE)
    pause_reason: str | None = Field(default=None)
    paused_at: datetime | None = Field(
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
    paused_kind: str | None = Field(default=None)
    # Pre-deploy saved max_concurrent_vms for PAUSE mode restore
    pre_pause_max_vms: int | None = Field(default=None)
    # Persisted runtime config — restored on startup so settings survive restarts
    runtime_max_concurrent_vms: int | None = Field(default=None)
    runtime_max_retries: int | None = Field(default=None)
    runtime_max_autonomous_tasks_per_hour: int | None = Field(default=None)
    runtime_max_global_autonomous_tasks_per_hour: int | None = Field(default=None)
    runtime_autonomous_research_interval_minutes: int | None = Field(default=None)
    runtime_autonomous_direction_cooldown_minutes: int | None = Field(default=None)
    runtime_autonomous_research_model: str | None = Field(default=None)
    runtime_autonomous_research_effort: str | None = Field(default=None)
    # Persisted circuit breaker states — survives restarts/deploys
    circuit_breaker_states: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted zone health/cooldown — prevents retry storms after restart
    zone_health_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted dict/list runtime config (task_type_models, agent_prompt_suffixes, etc.)
    runtime_config_json: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted anomaly detection state — occurrence counts and active adjustments survive restarts
    persistent_anomaly_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted direction failure streaks — holds survive restarts silently
    direction_streak_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Persisted pipeline events ring buffer — alert windows survive restarts
    pipeline_events_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Previous deploy tracking — populated when a new deploy supersedes the
    # current one. Lets RecentChangesContext builders compute the commit range
    # (prior_deploy_sha..current_sha) without a live git checkout.
    prior_deploy_sha: str | None = Field(default=None)
    prior_deployed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Timestamp when HALTED state was entered — used for auto-recovery timer
    halted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Optimistic-concurrency version (audit 2026-04-25 W1 / F4): bumped on
    # every write so concurrent mutations from different code paths can
    # detect lost-write hazards without holding the row lock for the whole
    # post-commit cycle.
    version: int = Field(
        default=0, sa_column=Column(Integer, nullable=False, server_default=text("0"))
    )


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
            SAEnum(
                LLMProviderName,
                name="llmprovider",
                values_callable=lambda obj: [e.value for e in obj],
            ),
            nullable=False,
            default=LLMProviderName.ANTHROPIC,
        ),
    )
    auth_mode: LLMAuthMode = Field(
        default=LLMAuthMode.OAUTH,
        sa_column=Column(
            SAEnum(
                LLMAuthMode,
                name="llmauthmode",
                values_callable=lambda obj: [e.value for e in obj],
            ),
            nullable=False,
            default=LLMAuthMode.OAUTH,
        ),
    )
    oauth_token: str | None = Field(
        default=None
    )  # Secret Manager resource name (or raw token for backward compat)
    claude_config_json: str | None = Field(
        default=None
    )  # ~/.claude.json content for onboarding bypass
    secret_blob_ref: str | None = Field(default=None)
    provider_metadata: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    enabled: bool = Field(default=True)
    total_tasks: int = Field(default=0)
    token_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )  # When the token was added (for age tracking)
    rate_limit_state: Dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Usage caps (null = unlimited).
    daily_task_cap: int | None = Field(default=None)
    max_concurrent_tasks: int | None = Field(default=None)
    auto_disable_after_rate_limits: int | None = Field(default=None)
    daily_task_count: int = Field(default=0)
    daily_task_count_reset_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    disabled_reason: str | None = Field(default=None)
    # Set when auto-disable trips (rate-limit threshold). Persisted so
    # recheck_disabled_accounts can re-enable after cooldown across
    # orchestrator restarts. Null for never-disabled or admin-disabled
    # accounts (admin disable doesn't auto-recover).
    disabled_at: datetime | None = Field(
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

    def is_in_rate_limit_cooldown(self) -> bool:
        """Return True if any model is still in rate-limit cooldown from persisted state.

        Reads self.rate_limit_state (JSONB, populated by _persist_rate_limit_state
        in max_plan_pool). Uses a conservative 4-hour upper-bound window — the
        maximum quarantine cooldown — to avoid a circular import of the precise
        _cooldown_seconds_for_info helper (max_plan_pool imports models).
        The in-memory pool check (max_plan_pool.account_in_rate_limit_cooldown)
        is authoritative when the pool is loaded; this method is a fallback for
        callers that have an ORM-loaded account but no live pool (e.g. tests,
        admin tooling). Returns False on any parse failure.
        """
        try:
            dm = (self.rate_limit_state or {}).get("disabled_models") or {}
            if not dm:
                return False
            now = _utcnow()
            for info in dm.values():
                at_str = info.get("at")
                if not at_str:
                    continue
                try:
                    at_dt = datetime.fromisoformat(str(at_str))
                    if at_dt.tzinfo is not None:
                        at_dt = at_dt.replace(tzinfo=None)
                except (ValueError, TypeError):
                    continue
                if (now - at_dt).total_seconds() < 4 * 3600:
                    return True
            return False
        except Exception:
            return False


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
    project_id: uuid.UUID | None = Field(
        default=None, foreign_key="project.id", index=True
    )
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
    sample_message: str | None = Field(default=None)
    # Lifecycle fields (added 0054). All nullable / default-0 / default-[]
    # so existing rows backfilled by the migration's server_default work
    # without a Python-side touch.
    last_spawned_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("task.id", ondelete="SET NULL"), nullable=True),
    )
    last_spawned_pr_number: int | None = Field(default=None)
    last_spawned_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    consecutive_failure_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    suspended_at: datetime | None = Field(
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
    audit_deferred_system_id: str | None = Field(default=None)

    __table_args__ = (
        Index("ix_residual_case_status_last_seen", "status", "last_seen_at"),
        Index(
            "ix_residualremediationcase_project_status_spawned",
            "project_id",
            "status",
            "last_spawned_at",
        ),
        UniqueConstraint(
            "project_id",
            "fingerprint",
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
    commit_hash: str | None = None
    repo_url: str | None = None
    pr_url: str | None = None

    # For visual feedback or logs
    storage_uri: str | None = None
    logs: str | None = None

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
    recommended_mode: str | None = None

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

    source_task_id: uuid.UUID | None = Field(
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
        UniqueConstraint(
            "project_id", "system_id", name="uq_system_maturity_project_system"
        ),
    )


# --- Regression-case watchdog thresholds ---
# Tunable via runtime_config using the same-named lowercase keys.
# REGRESSION_WATCHDOG_ACTIVE_STALE_HOURS: Hours since updated_at before an ACTIVE
#   case is considered stuck and escalated to NEEDS_MANUAL_INTERVENTION.  An ACTIVE
#   case goes stale when an investigation completes with low/no confidence and no
#   remediation task is spawned.
# REGRESSION_WATCHDOG_MERGED_STALE_HOURS: Hours since updated_at before a MERGED
#   case is considered stuck.  A MERGED case goes stale when the recovery scan
#   cannot evaluate it (e.g. signal_snapshot_at_merge is empty).
REGRESSION_WATCHDOG_ACTIVE_STALE_HOURS = 24
REGRESSION_WATCHDOG_MERGED_STALE_HOURS = 6


class RegressionCaseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REMEDIATING = "REMEDIATING"
    MERGED = "MERGED"
    CLOSED = "CLOSED"
    NEEDS_MANUAL_INTERVENTION = "NEEDS_MANUAL_INTERVENTION"
    # Investigation concluded the signal does not represent a real regression
    # (intended behavior change, expected variance, statistical noise on a
    # small sample, or a known-incomplete pipeline step). Spawning is
    # suppressed until ``false_positive_until_at`` lapses, after which the
    # next dispatcher tick re-arms the case to ACTIVE so a genuine recurrence
    # of the same signal can still be diagnosed.
    FALSE_POSITIVE = "FALSE_POSITIVE"


# Distinguishes the path that drove a case into NEEDS_MANUAL_INTERVENTION so the
# admin UI can render the right badge ('Max attempts' vs 'Diverged diagnoses'
# vs 'Stale (watchdog)') and the operator timeline survives re-wording of the
# Alert.message string. Stored as a free-form String column rather than an
# enum so we don't need a SQL constraint migration to add new reasons.
ESCALATION_REASON_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
ESCALATION_REASON_INVESTIGATIONS_DIVERGED = "investigations_diverged"
ESCALATION_REASON_STALE_ACTIVE = "stale_active"
ESCALATION_REASON_STALE_MERGED = "stale_merged"
# Meta-investigation escalation reasons — set when the dispatcher spawns a
# cross-layer / concentration / fingerprint / recurrence investigation so
# operators can distinguish these from manual-intervention triggers in the UI.
ESCALATION_REASON_ESCALATED_CROSS_LAYER = "escalated_cross_layer"
ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION = "escalated_layer_concentration"
ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT = "escalated_cross_case_fingerprint"
ESCALATION_REASON_ESCALATED_RECURRENCE = "escalated_recurrence"
REGRESSION_ESCALATION_REASONS: frozenset[str] = frozenset(
    {
        ESCALATION_REASON_ATTEMPTS_EXHAUSTED,
        ESCALATION_REASON_INVESTIGATIONS_DIVERGED,
        ESCALATION_REASON_STALE_ACTIVE,
        ESCALATION_REASON_STALE_MERGED,
        ESCALATION_REASON_ESCALATED_CROSS_LAYER,
        ESCALATION_REASON_ESCALATED_LAYER_CONCENTRATION,
        ESCALATION_REASON_ESCALATED_CROSS_CASE_FINGERPRINT,
        ESCALATION_REASON_ESCALATED_RECURRENCE,
    }
)

# Stored in last_remediation_result when the watchdog auto-closes a
# NEEDS_MANUAL_INTERVENTION case after its underlying signal recovered without
# operator action. Distinguishes unattended recoveries from "closed" (post-merge
# recovery) and "merged" (PR merged) so the recently-closed admin views can
# render the right badge.
CLOSE_REASON_RECOVERED_UNATTENDED = "recovered_unattended"
# Stored in last_remediation_result when auto-close detects that the signal
# recovered but the improvement is below both relative and absolute floors —
# the fix nudged the metric just enough to not trip but is likely to re-trip.
CLOSE_REASON_RECOVERED_MARGINAL = "recovered_marginal"


class RegressionRemediationCase(SQLModel, table=True):
    """Persistent record for a regression fingerprint detected across agent runs.

    Created or updated whenever a regression is detected; tracks the status
    of remediation efforts and links to the most recent remediation task.
    Lifecycle: ACTIVE → MERGED (PR merged, awaiting signal recheck) →
    CLOSED (signal recovered) or back to ACTIVE (signal still tripped).
    Status NEEDS_MANUAL_INTERVENTION suppresses dispatcher spawning until
    cleared via admin endpoint; reached when attempts >= 3 with no recovery.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    regression_class: str = Field(max_length=512)
    status: str = Field(
        default=RegressionCaseStatus.ACTIVE,
        sa_column=Column(
            String, nullable=False, server_default=RegressionCaseStatus.ACTIVE
        ),
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
    sample_message: str | None = Field(default=None)
    last_remediation_task_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("task.id", ondelete="SET NULL"), nullable=True),
    )
    # Prompt fingerprint active when this regression was first spawned for remediation.
    # Lets operators ask: "did this regression appear before or after prompt change X?"
    prompt_fingerprint_at_spawn: str | None = Field(default=None, max_length=12)
    # Close-loop tracking fields (added 0074). All nullable / default-0 / default-[]
    # so existing rows backfilled by the migration server_default work without a
    # Python-side touch. The dispatcher writes attempts/snapshots at spawn; the
    # task_completion hook writes last_remediation_pr_number/result on merge;
    # the per-tick recovery scan appends to fingerprint_to_fix_log on CLOSE.
    attempts: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    # Number of one-shot autonomous escalations the watchdog has fired before
    # promoting the case to NEEDS_MANUAL_INTERVENTION. Capped at 1 today: when
    # the original investigate task is still active but the case has gone
    # stale, the dispatcher gets ONE additional autonomous attempt at the
    # remediation blocker before paging the operator.
    auto_escalation_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    last_remediation_pr_number: int | None = Field(default=None)
    last_remediation_result: str | None = Field(default=None)
    last_failure_class: str | None = Field(default=None)
    signal_snapshot_at_spawn: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    signal_snapshot_at_merge: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    fingerprint_to_fix_log: List[Dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'")),
    )
    needs_manual_intervention_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Timestamp when an operator first acknowledged the manual-intervention
    # escalation. NULL for unacknowledged cases and for historical cases
    # predating this column. Set once via the acknowledge endpoint; idempotent
    # (never overwritten once stamped). Enables MTTI metric:
    # needs_manual_intervention_at → operator_acknowledged_at.
    operator_acknowledged_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Timestamp when the underlying signal first crossed its threshold during
    # the most recent dispatcher pass. Separate from first_seen_at (case
    # creation) and last_remediation_task_id spawn time. Enables time-to-detect
    # metric: signal_first_crossed_at → first_seen_at gap.
    signal_first_crossed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Timestamp when the underlying signal FIRST crossed threshold. Enables
    # MTTD = first_seen_at → case.created_at. Null for cases opened before
    # this column was added, and for signals without a durable first-seen record.
    signal_first_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Path that drove this case into NEEDS_MANUAL_INTERVENTION; one of
    # REGRESSION_ESCALATION_REASONS. Nullable for legacy rows pre-dating
    # the field and for cases that have not been escalated yet.
    escalation_reason: str | None = Field(
        default=None,
        sa_column=Column(String, nullable=True),
    )
    # Durable recovery clock for the auto-close path on
    # NEEDS_MANUAL_INTERVENTION cases. The watchdog sets this when the
    # underlying signal first reads healthy and uses the difference between
    # now and this timestamp to enforce the
    # ``regression_recovered_unattended_window_hours`` stable window before
    # auto-closing the case. Reset to NULL when the signal trips again, on
    # case re-arm/closure, and when the operator presses Clear — mirroring
    # the .pop() semantics of the deprecated process-local
    # ``_signal_healthy_since`` dict.
    signal_healthy_since_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Suppression clock for FALSE_POSITIVE-status cases. When the investigate
    # agent reports verdict=="not_a_regression" on a regression-dispatcher
    # spawn, completion sets status=FALSE_POSITIVE and stamps this timestamp
    # to now+TTL. The dispatcher silently skips spawn while this is in the
    # future; once it lapses, the next tick re-arms to ACTIVE so genuine
    # recurrence after expiry is still caught. NULL on every other status.
    false_positive_until_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Free-form reason captured from the investigate report's verdict_reason
    # so operators can audit auto-suppressions. Truncated to 500 chars.
    false_positive_reason: str | None = Field(
        default=None,
        sa_column=Column(String, nullable=True),
    )
    # Per-layer symptom log: {layer_name: [{fingerprint, observed_at, sample}]}.
    # Written by the dispatcher when classify_layers() matches; bounded to 10
    # entries per layer so the column doesn't grow unboundedly.
    # Migration: alembic 0085 adds this column with server_default='{}').
    symptoms_by_layer: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    # Timestamp when the dispatcher first spawned an investigate Task for this case.
    # Null for cases pre-dating this column. Never overwritten on re-spawns so
    # operators always see the original detection-to-action latency.
    investigate_spawned_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Failure-driven re-arm flag. Set by re_arm(fast_track=True) (default) when
    # the prior remediation task failed (review-rejected, CI-failed, merge-blocked).
    # The dispatcher bypasses the 6h class-level cooldown when this flag is set
    # AND attempts > 0, then clears it after the next investigate spawn so the
    # bypass applies only once per failure. Recovery-scan re-arm (post-MERGED
    # recurrence) passes fast_track=False and continues to honor the cooldown.
    fast_track_next_spawn: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    # Brief excerpt (≤500 chars) of the prior remediation's terminal message —
    # review feedback, CI error tail, or merge-conflict note. Used to populate
    # the next investigation's prompt with a 'PRIOR REMEDIATION FAILURE CONTEXT'
    # block so the agent can choose a different angle. Falls back to fetching
    # the prior task's latest_status_message at spawn time when null.
    last_failure_summary: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    # Timestamp when the most recent marginal-recovery follow-up investigate task
    # was spawned for this case. Used as a durable cooldown anchor: the dispatcher
    # skips spawning a new follow-up if now - marginal_followup_spawned_at is
    # within the configured cooldown window (default 72 h). Null for cases that
    # have never had a marginal follow-up or pre-date this column.
    marginal_followup_spawned_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Timestamp when the most recent compliance follow-up investigate task was
    # spawned for this case. Acts as a durable cooldown anchor: the dispatcher
    # skips spawning a new compliance follow-up if now - compliance_followup_spawned_at
    # is within the configured cooldown window (default 72 h). Null for cases that
    # have never triggered a compliance follow-up or pre-date this column.
    compliance_followup_spawned_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    # Running count of compliance follow-up tasks spawned for this case.
    # The dispatcher compares this against a max cap to prevent a cross-layer
    # mandate that is permanently unsatisfiable (e.g. requires unmodifiable
    # CI_WORKFLOW layer) from re-spawning a compliance-followup indefinitely.
    # Incremented each time _run_compliance_followup_once spawns a task for
    # this case; never decremented. Migration: alembic 0102.
    compliance_followup_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    # Attribution for the PR/commit that introduced this regression. Set when
    # a new case is first created or re-tripped after CLOSED. Null for legacy
    # rows and cases where attribution could not be determined.
    introducing_pr_number: int | None = Field(default=None)
    introducing_commit_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "regression_class",
            name="ux_regressionremediationcase_project_class",
        ),
        Index(
            "ix_regressionremediationcase_project_status_last_seen",
            "project_id",
            "status",
            "last_seen_at",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'REMEDIATING', 'MERGED', 'CLOSED', 'NEEDS_MANUAL_INTERVENTION', 'FALSE_POSITIVE')",
            name="ck_regressionremediationcase_status",
        ),
    )

    @property
    def time_to_detect_sec(self) -> float | None:
        """Seconds between signal crossing threshold and this case being opened.

        Prefers signal_first_crossed_at (current dispatcher writes both fields;
        the crossed-at column is the canonical time-to-detect anchor). Falls
        back to signal_first_seen_at for rows written before signal_first_crossed_at
        existed. Returns None when neither is available.
        """
        anchor = self.signal_first_crossed_at or self.signal_first_seen_at
        if anchor is None:
            return None
        return (self.first_seen_at - anchor).total_seconds()

    @property
    def time_to_investigate_sec(self) -> int | None:
        """Seconds between first_seen_at and the first investigate Task spawn.

        Returns None when investigate_spawned_at is missing (legacy rows) or
        when the delta is negative (clock skew guard). Integer seconds.
        """
        if self.investigate_spawned_at is None or self.first_seen_at is None:
            return None
        delta = int((self.investigate_spawned_at - self.first_seen_at).total_seconds())
        return delta if delta >= 0 else None

    def _current_episode_investigate_start(self) -> datetime | None:
        """Return the investigation-start anchor for the current fix episode.

        For the first episode this equals investigate_spawned_at.  After each
        completed recovery cycle (mark_closed writes a recovered_at entry) the
        anchor advances to that cycle's recovered_at, so per-episode
        investigate→merge durations are not inflated by earlier cycles.
        This mirrors the ep_investigate_start advancement applied by
        get_regression_mtta / get_regression_mtta_data.
        """
        anchor = self.investigate_spawned_at
        for entry in self.fingerprint_to_fix_log or []:
            if not isinstance(entry, dict):
                continue
            recovered_raw = entry.get("recovered_at")
            if recovered_raw:
                try:
                    anchor = datetime.fromisoformat(
                        str(recovered_raw).replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except (ValueError, TypeError):
                    pass
        return anchor

    def mark_merged(
        self,
        pr_number: int,
        signal_snapshot: Dict[str, Any],
        layers_targeted: List[str] | None = None,
        recommended_unmodifiable_layers: List[str] | None = None,
        *,
        spawn_was_cross_layer: bool = False,
        spawn_layers_enumerated: List[str] | None = None,
    ) -> None:
        """Transition ACTIVE → MERGED after remediation PR merges.

        Captures the post-merge signal snapshot and appends a fix-log entry so
        the recovery scan can verify the regression resolved.
        Falls back to signal_snapshot_at_spawn when the caller passes an empty
        snapshot so run_recovery_once can evaluate the MERGED→CLOSED transition
        regardless of which call site triggered the merge.

        ``layers_targeted`` (optional) records which canonical infrastructure
        layers the remediation PR's changed files mapped to (subset of the 5
        layers in regression_layer_classifier.ALL_LAYERS). When None or empty,
        the entry is written without the key — readers must treat absence as
        "unclassified", not "no layers". Existing fix-log entries written
        before this field existed remain readable everywhere.

        ``recommended_unmodifiable_layers`` (optional) records layers the agent
        identified as needing changes but could not modify (e.g. CI_WORKFLOW).
        Credited toward the >=2-layer compliance total by the watchdog so a fix
        that correctly documents an unmodifiable layer is not flagged non-compliant.

        ``spawn_was_cross_layer`` / ``spawn_layers_enumerated`` record the
        dispatcher's cross-layer escalation context at spawn time so the
        compliance metric can compare layers observed vs layers touched.
        Only written when truthy; absence means non-cross-layer or legacy entry.
        """
        if not signal_snapshot:
            signal_snapshot = dict(self.signal_snapshot_at_spawn or {})
        self.signal_snapshot_at_merge = signal_snapshot
        self.last_remediation_pr_number = pr_number
        self.last_remediation_result = "merged"
        ep_start = self._current_episode_investigate_start()
        log_entry: Dict[str, Any] = {
            "fingerprint": self.regression_class,
            "fix_pr_number": pr_number,
            "merged_at": _utcnow().isoformat(),
        }
        if ep_start is not None:
            log_entry["episode_investigate_started_at"] = ep_start.isoformat()
        if layers_targeted:
            log_entry["layers_targeted"] = list(layers_targeted)
        if recommended_unmodifiable_layers:
            log_entry["recommended_unmodifiable_layers"] = list(
                recommended_unmodifiable_layers
            )
        if spawn_was_cross_layer:
            log_entry["spawn_was_cross_layer"] = True
            log_entry["spawn_layers_enumerated"] = list(spawn_layers_enumerated or [])
        self.fingerprint_to_fix_log = [
            *list(self.fingerprint_to_fix_log or []),
            log_entry,
        ]
        self.status = RegressionCaseStatus.MERGED

    def mark_closed(
        self,
        post_merge_value: float | None = None,
        recovered_at: str | None = None,
    ) -> None:
        """Transition MERGED → CLOSED when the signal shows recovery.

        Idempotent: if already CLOSED, returns without side-effects.
        When ``post_merge_value`` is provided, appends a recovery entry to
        ``fingerprint_to_fix_log`` so the close-loop log captures the
        post-merge signal reading alongside the original fix PR.
        """
        if self.status == RegressionCaseStatus.CLOSED:
            return
        self.last_remediation_result = "closed"
        self.status = RegressionCaseStatus.CLOSED
        if post_merge_value is not None:
            entry: Dict[str, Any] = {
                "fingerprint": self.regression_class,
                "recovered_at": recovered_at or _utcnow().isoformat(),
                "post_merge_value": post_merge_value,
            }
            self.fingerprint_to_fix_log = [
                *list(self.fingerprint_to_fix_log or []),
                entry,
            ]

    def prior_attempt_context_suffix(self) -> str:
        """Return a formatted prior-attempt context block for re-spawned prompts.

        Returns an empty string when attempts < 2 so first-attempt prompts are
        unchanged.  For attempts >= 2 the block names the prior PR (if any) and
        includes the post-merge signal value so the agent has a concrete anchor
        rather than re-investigating from scratch.
        """
        if (self.attempts or 0) < 2:
            return ""
        lines: list[str] = [
            "",
            "PRIOR ATTEMPT CONTEXT",
            f"This regression has been investigated {self.attempts} time(s) without recovering the signal.",
        ]
        pr = self.last_remediation_pr_number
        if pr is not None:
            lines.append(
                f"Prior remediation PR: #{pr}. Read its diff to understand what was already tried "
                "before forming a new hypothesis."
            )
        snap = self.signal_snapshot_at_merge or {}
        post_val = snap.get("current_rate")
        if post_val is not None:
            lines.append(f"Signal value after prior fix merged: {post_val:.1%}.")
        elif snap.get("crash_count") is not None:
            lines.append(f"Crash count at time of prior fix: {snap['crash_count']}.")
        lines.append(
            "Do NOT converge on the same diagnosis as prior attempts. "
            "Approach from a different angle — look for a deeper root cause or a "
            "different contributing factor that the prior fix missed."
        )
        return "\n".join(lines)

    def re_arm(self, max_attempts: int = 3, *, fast_track: bool = True) -> None:
        """Handle remediation failure or post-MERGED recurrence.

        Increments the attempt counter and either resets to ACTIVE for another
        spawn or escalates to NEEDS_MANUAL_INTERVENTION when the cap is reached.
        Idempotent once NEEDS_MANUAL_INTERVENTION is reached.

        ``fast_track`` defaults to True: failure-driven callers (the
        ``_close_loop_regression_case_on_failure`` task-lifecycle hook) call
        ``re_arm()`` with no args and opt in to the dispatcher's cooldown
        bypass on the next cycle. Recovery-scan callers (post-MERGED
        recurrence) pass ``fast_track=False`` to preserve the 6h cooldown,
        since a still-tripped post-merge signal needs time to settle rather
        than an immediate re-investigation.

        The fast-track flag is suppressed when the case escalates to
        NEEDS_MANUAL_INTERVENTION so the dispatcher does not attempt a bypass
        on a row that should be quiet.
        """
        if self.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION:
            return
        self.attempts = (self.attempts or 0) + 1
        self.last_remediation_result = "failed"
        if self.attempts >= max_attempts:
            self.status = RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
            self.needs_manual_intervention_at = _utcnow()
            self.escalation_reason = ESCALATION_REASON_ATTEMPTS_EXHAUSTED
        else:
            self.status = RegressionCaseStatus.ACTIVE
            if fast_track:
                self.fast_track_next_spawn = True

    def escalate_for_divergence(self, detail: str | None = None) -> None:
        """Escalate to NEEDS_MANUAL_INTERVENTION because successive investigations diverged.

        Idempotent once already NEEDS_MANUAL_INTERVENTION (preserves the prior
        escalation_reason — first cause wins). Caller is responsible for
        persisting the case and emitting the operator-facing Alert.
        """
        del detail  # reserved for future use; kept in signature for caller clarity
        if self.status == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION:
            return
        self.status = RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION
        self.needs_manual_intervention_at = _utcnow()
        self.escalation_reason = ESCALATION_REASON_INVESTIGATIONS_DIVERGED

    def record_spawn(
        self,
        signal_snapshot: Dict[str, Any],
        task_id: "uuid.UUID",
    ) -> None:
        """Update case fields when a new spawn happens (existing case path).

        Refreshes signal_snapshot_at_spawn with the latest signal data,
        increments hit_count, and links to the new remediation task.
        Call on an existing case before session.add(); the dispatcher uses
        this to keep the snapshot current across repeat trips.
        """
        self.signal_snapshot_at_spawn = signal_snapshot
        self.hit_count = (self.hit_count or 0) + 1
        self.last_seen_at = _utcnow()
        self.last_remediation_task_id = task_id

    @classmethod
    def create_for_spawn(
        cls,
        project_id: "uuid.UUID",
        regression_class: str,
        signal_snapshot: Dict[str, Any],
        task_id: "uuid.UUID",
    ) -> "RegressionRemediationCase":
        """Factory for the first-time spawn path.

        Returns an unsaved instance ready to be passed to session.add().
        Sets hit_count=1 and captures signal_snapshot_at_spawn so the
        recovery scan has a baseline to compare against after remediation.
        """
        now = _utcnow()
        return cls(
            project_id=project_id,
            regression_class=regression_class,
            status=RegressionCaseStatus.ACTIVE,
            signal_snapshot_at_spawn=signal_snapshot,
            last_remediation_task_id=task_id,
            last_seen_at=now,
            hit_count=1,
        )


class AnomalyFingerprintState(SQLModel, table=True):
    """Durable per-project anomaly fingerprint record.

    Keyed by (project_id, fingerprint_key) so each distinct anomaly type
    for a project has exactly one row.  The in-memory persistent_anomalies
    dict in shared_state.py acts as a hot cache; this table is the source of
    truth that survives pod restarts, deploys, and OOM kills.

    Usage pattern:
      - Write: upsert after each anomaly detection cycle (see anomaly_detection.py).
      - Read: hydrate shared_state.persistent_anomalies on startup (see main.py).
      - Fallback: pipeline_regression_dispatcher reads this table directly when
        the in-memory cache is empty after a restart.
    """

    __tablename__ = "anomalyfingerprintstate"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "fingerprint_key",
            name="ux_anomalyfingerprintstate_project_key",
        ),
        Index("ix_anomalyfingerprintstate_project_id", "project_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=False)
    fingerprint_key: str = Field(max_length=512)
    occurrence_count: int = Field(default=1)
    first_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    last_seen_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    severity: str | None = Field(default=None, max_length=64)
    latest_details: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, onupdate=_utcnow),
    )


class DispatcherCycleRecord(SQLModel, table=True):
    """Durable record of a single pipeline_regression_dispatcher cycle.

    Replaces the process-local ``_dispatcher_cycle_records`` deque so cycle
    throughput, signal-trip counts, and rejection-by-reason aggregates survive
    worker restarts and remain consistent across multi-worker deployments.

    Retention is bounded to ~30 days by a periodic prune call inside
    ``run_once`` (see ``_prune_old_dispatcher_cycle_records``).  Older rows
    are deleted opportunistically rather than via a server-side TTL trigger
    so behavior is identical on Postgres and the SQLite test fixture.
    """

    __tablename__ = "dispatchercyclerecord"
    __table_args__ = (
        Index(
            "ix_dispatchercyclerecord_project_started",
            "project_id",
            "cycle_started_at",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # ON DELETE CASCADE: cycle records are per-project telemetry with no
    # value once the project is gone, and the postgres conftest cleanup
    # path uses straight DELETE FROM project (no per-table sweep), so the
    # FK must cascade or teardown fails.
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    cycle_started_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    cycle_finished_at: datetime = Field(
        sa_column=Column(NaiveUTCDateTime, nullable=False),
    )
    signals_evaluated: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    signals_tripped: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    spawns_emitted: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    rejections_by_reason: Dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")),
    )
    mttd_p50_sec: float | None = Field(default=None)
    marginal_recoveries_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    confirmed_recoveries_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    avg_prevention_rate_7d: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )


class ReleaseVersion(SQLModel, table=True):
    """Release-version lifecycle record for automain → main promotion gating.

    A "version" is the span of automain between two promotions.  The strategic
    layer drives phase transitions:

      build      — normal feature work on automain
      stabilize  — shift work-mix to audit/cleanup/hardening before qualify
      qualify    — heavy gate running once per version (Phase 3)
      promote    — transition to next version after successful promotion

    Only one row per project should have ``status="open"`` at a time.
    ``version_number`` is monotonically increasing per project.

    Columns named *_sha are nullable per migration linter rules.
    All non-null columns carry ``server_default`` for safe backfill.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    # Monotonically increasing per project (1-based).
    version_number: int = Field(
        default=1,
        sa_column=Column(Integer, nullable=False, server_default=text("1")),
    )

    # phase: build | stabilize  (qualify / promote are transient orchestrator ops,
    # not durable DB phases — they are reflected by status transitions).
    phase: str = Field(
        default="build",
        sa_column=Column(String(32), nullable=False, server_default="build"),
    )

    # status: open | promoted | abandoned
    status: str = Field(
        default="open",
        sa_column=Column(String(32), nullable=False, server_default="open"),
    )

    # The version's goal — a point-in-time snapshot of
    # ProblemModel.intent.current_focus captured when this version opened.
    # Frozen for the version's lifetime so each version keeps a stable record
    # of what it was for, even after current_focus drifts on.  Nullable:
    # pre-0111 rows and projects with no current_focus carry no goal.
    goal: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    opened_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    # Stamped when phase changes (build → stabilize, etc.).
    phase_changed_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Stamped when status transitions to "promoted".
    promoted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # SHA columns are nullable per migration linter rules.
    # base_sha: automain HEAD when this version was opened.
    base_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # promoted_sha: automain HEAD at the time of successful promotion.
    promoted_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # Structured result of the last qualification run (Phase 3).
    # Shape: {score, sub_scores, blocker, attempted_at, …}
    last_qualification: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Total number of qualification attempts on this version (Phase 3).
    qualification_attempts: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )

    # Free-form reasoning text from the strategic agent explaining the latest
    # phase decision.
    phase_reasoning: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    # Durable deduplication anchor for calcification alerting.
    # Set to the blocker code when _check_version_calcification posts an alert.
    # The next sweep compares this field against the current blocker: if they
    # match, the alert has already been posted for this stall and is skipped;
    # if they differ (or the field is NULL), a new alert is warranted.
    # Storing the dedup state here avoids relying solely on Alert row existence
    # (which can be lost when an operator clears alerts).
    calcification_alerted_blocker: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
    )

    # Durable deduplication anchor for build-stall alerting.
    # Set to the build-phase START reference time (phase_changed_at, falling
    # back to opened_at) when the build-stall sweep posts an alert for a version
    # sitting too long in the 'build' phase without advancing. The next sweep
    # compares this field against the current build-phase reference time: if
    # they match, the alert has already been posted for this stall and is
    # skipped; if they differ (or the field is NULL), a new alert is warranted.
    # Because a version can move build → stabilize → build again (each
    # transition re-stamps phase_changed_at), storing the reference time lets
    # the dedup reset naturally when a fresh build phase begins while never
    # re-firing for the same stall. Storing the dedup state here avoids relying
    # solely on Alert row existence (which can be lost when an operator clears
    # alerts).
    build_stall_alerted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Durable deduplication anchor for stabilize-stall alerting.
    # Set to the stabilize-phase START reference time (phase_changed_at,
    # falling back to opened_at) when the stabilize-stall sweep posts an
    # alert for a version sitting too long in 'stabilize' without requesting
    # qualification.  Same dedup semantics as build_stall_alerted_at: storing
    # the reference time lets the dedup reset naturally when a fresh stabilize
    # phase begins (phase_changed_at re-stamped) while never re-firing for the
    # same stall.
    stabilize_stall_alerted_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    # Durable deduplication anchor for promotion-readiness alerting.
    # Set to True when a promotion_readiness_reached notification fires for
    # the current ready window. Reset to False when compute_promotion_readiness
    # returns ready=False so the next not-ready→ready recovery can notify again.
    promotion_ready_notified: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
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
        Index("ix_releaseversion_project_status", "project_id", "status"),
        UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_releaseversion_project_version",
        ),
    )


class QualificationVerdict(SQLModel, table=True):
    """Immutable per-verdict history of release-qualifier finalize outcomes.

    Phase 4 ("Calibration") of the automain LTS design.  ``ReleaseVersion.
    last_qualification`` is OVERWRITTEN on every finalize, so only the latest
    verdict survives there — and while ``promotion_enabled`` is False the open
    version is never closed, so cross-version history cannot be reconstructed.
    This table appends ONE row per finalized qualification verdict that is never
    overwritten, so the dry-run track record is durably retained and can be
    aggregated into a promotion-readiness recommendation
    (``release_qualifier.compute_promotion_readiness``).

    Additive only: it lives ALONGSIDE ``last_qualification`` and does not change
    how that field is written.  Writes are best-effort — a failure to record
    must never break or roll back a qualification or promotion.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    release_version_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(),
            ForeignKey("releaseversion.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    # The automain HEAD SHA that was scored.  Nullable per migration linter rules.
    candidate_sha: str | None = Field(
        default=None,
        sa_column=Column(String(40), nullable=True),
    )

    # Composite promotion confidence in [0,1].
    confidence: float = Field(
        default=0.0,
        sa_column=Column(Float, nullable=False, server_default=text("0")),
    )

    # Per-dimension sub-scores: {audit, test_erosion, smoketest}.
    sub_scores: Dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )

    # Hard-blocker code (compute_composite), or NULL when none fired.
    blocker: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
    )

    # Whether the candidate cleared the promotion threshold.
    would_promote: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )

    # The runtime_config["promotion_enabled"] state AT THE TIME of this verdict.
    promotion_enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )

    # Terminal outcome: dry_run | not_promoted | promoted | promotion_failed.
    terminal_status: str = Field(
        default="",
        sa_column=Column(String(32), nullable=False, server_default=""),
    )

    # The promotion threshold in force when this verdict was scored.
    threshold: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
    )

    # When the qualification started / finalized (carried from the verdict).
    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )
    finalized_at: datetime | None = Field(
        default=None,
        sa_column=Column(NaiveUTCDateTime, nullable=True),
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(NaiveUTCDateTime, nullable=False, server_default=func.now()),
    )

    __table_args__ = (
        Index(
            "ix_qualificationverdict_project_created",
            "project_id",
            "created_at",
        ),
    )
