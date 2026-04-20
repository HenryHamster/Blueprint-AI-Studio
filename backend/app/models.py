from enum import Enum
from typing import Optional, Dict, Any, List
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, String, Integer, Index, text, event
from sqlalchemy.dialects.postgresql import JSONB
import logging
import uuid
from datetime import datetime, timezone
from app.time_utils import _utcnow

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


# --- Models ---

class AllowedUser(SQLModel, table=True):
    """
    Allowlist of users permitted to access the Blueprint application.
    Roles are managed here instead of Firebase custom claims.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(index=True, unique=True)
    firebase_uid: Optional[str] = Field(default=None, index=True)
    role: str = Field(default="user")  # "admin" or "user"
    display_name: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    last_login_at: Optional[datetime] = None


class UserIntegration(SQLModel, table=True):
    """
    Stores third-party integrations for a user, such as GitHub App installations.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True, unique=True)

    # GitHub App Installation ID
    github_installation_id: Optional[str] = None
    github_account_name: Optional[str] = None

    # GitHub Fine-Grained Personal Access Token (Alternative to App Installation)
    github_pat: Optional[str] = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

class Project(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    user_id: str = Field(index=True)
    status: ProjectStatus = Field(default=ProjectStatus.DRAFT, sa_column=Column(String, default=ProjectStatus.DRAFT))
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)

    # Autonomous improvement: user-defined high-level directions for research agents
    # Includes directions with status: 'active', 'paused', 'proposed', 'rejected', 'expired'
    # Auto-pause diagnosis keys (set when an investigate task is spawned to
    # diagnose repeated code failures — see DIAGNOSIS_* constants above):
    #   diagnosis_in_progress: bool, diagnosis_task_id: str, diagnosis_started_at: ISO str
    directions: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, default=[]))
    # Strategic product vision maintained by the strategic research agent
    vision: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=True, default={}))
    # Living architectural description maintained by design doc update agent
    design_doc: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=True, default={}))
    # Toggle autonomous research/task generation
    autonomous_enabled: bool = Field(default=True)
    # Self-repo flag: enables rollback monitoring, extended windows, infra soft gates
    # Set True when github_owner/github_repo matches Blueprint's own repo
    is_self_repo: bool = Field(default=False)
    # Build configuration: directories, commands, git identity for the agent
    build_config: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))

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
    role: str = Field(default="member")  # "owner", "member", "viewer"
    created_at: datetime = Field(default_factory=_utcnow)


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

    created_by: str = Field(default="USER") # "USER", "ORCHESTRATOR", "AGENT_{ID}"
    launched_by_user_id: Optional[str] = Field(default=None)  # Firebase UID of the user who launched this task (for launcher-pays billing)
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

    # Stores parsed plan output from plan-type tasks (for debugging/admin visibility)
    plan_output: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))

    # Agent Heartbeat mechanism
    last_heartbeat: Optional[datetime] = Field(default=None)

    priority: int = Field(default=50, sa_column=Column(Integer, nullable=False, server_default=text("50")))

    attempt_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=_utcnow)
    started_at: Optional[datetime] = Field(default=None)
    # Auto-populated on INSERT (default_factory) and UPDATE (onupdate). Consumed by
    # reaper.py's EVALUATING stale sweep and rollup_management.py age calculations
    # so "last touched" can be distinguished from "first started".
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column_kwargs={"onupdate": _utcnow},
    )
    archived: bool = Field(default=False)

    # VM lifecycle timing for cost tracking
    vm_provisioned_at: Optional[datetime] = Field(default=None)
    vm_started_at: Optional[datetime] = Field(default=None)
    vm_stopped_at: Optional[datetime] = Field(default=None)
    vm_spot: bool = Field(default=False)

    @property
    def first_payload(self) -> Dict[str, Any]:
        """First payload dict, defaulting to empty dict if none exist."""
        return (self.payloads or [{}])[0]

    __table_args__ = (
        Index("ix_task_status_priority", "status", "priority"),
        Index("ix_task_status_priority_created", "status", "priority", "created_at"),
        Index("ix_task_project_status_created", "project_id", "status", "created_at"),
        Index("ix_task_context_gin", "context", postgresql_using="gin"),
        Index("ix_task_depends_on_gin", "depends_on", postgresql_using="gin"),
    )

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

    timestamp: datetime = Field(default_factory=_utcnow)
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

@event.listens_for(Task.status, "set")
def _on_status_set(target: Task, value, oldvalue, initiator):
    if value and isinstance(oldvalue, str) and value != oldvalue:
        entry = {"t": _utcnow().isoformat(), "msg": f"Status changed: {oldvalue} → {value}"}
        target.status_log = ((target.status_log or []) + [entry])[-90:]
        # Notify JWT revocation when task reaches terminal state
        if value in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            task_id = str(target.id)
            for cb in _on_task_terminal_callbacks:
                try:
                    cb(task_id)
                except Exception as e:
                    logger.error(f"[MODELS] Terminal callback failed for task {task_id}: {e}", exc_info=True)


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
    task_id: Optional[uuid.UUID] = None
    created_at: datetime = Field(default_factory=_utcnow)
    dismissed_at: Optional[datetime] = None

    __table_args__ = (
        Index("ix_alert_task_id_dismissed", "task_id", "dismissed_at"),
    )


class OrchestratorErrorAck(SQLModel, table=True):
    """Persistent acknowledgement of an orchestrator error bucket.

    key is "exception_class|source" for a specific bucket, or "*" for a
    global ack covering every bucket whose last_seen_iso <= acknowledged_at.
    """
    key: str = Field(primary_key=True)
    acknowledged_at: datetime = Field(default_factory=_utcnow)
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
    from_type: str = Field(default="user")  # "user" | "agent" | "strategic"
    from_task_id: Optional[uuid.UUID] = Field(default=None, foreign_key="task.id")
    content: str = Field(default="")
    context: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=True, default={}))
    status: str = Field(default="open")  # "open" | "resolved"
    resolution: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)

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
    deployed_at: datetime = Field(default_factory=_utcnow)
    status: str = Field(default=DeploymentStatus.STABLE, sa_column=Column(String, default=DeploymentStatus.STABLE))
    last_known_good_revision: str = Field(default="")
    last_known_good_sha: str = Field(default="")
    rolled_back_from_sha: Optional[str] = Field(default=None)
    rollback_reason: Optional[str] = Field(default=None)
    rollback_count: int = Field(default=0)           # consecutive rollbacks (reset on STABLE)
    pause_reason: Optional[str] = Field(default=None)
    paused_at: Optional[datetime] = Field(default=None)
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
    halted_at: Optional[datetime] = Field(default=None)


class MaxPlanAccount(SQLModel, table=True):
    """
    Persisted Max plan account for Claude Code OAuth token pooling.
    Each account holds a long-lived OAuth token (sk-ant-oat01-...) generated via `claude setup-token`.
    Active task counts are tracked in-memory only.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(index=True)
    name: str
    oauth_token: str  # Secret Manager resource name (or raw token for backward compat)
    claude_config_json: Optional[str] = Field(default=None)  # ~/.claude.json content for onboarding bypass
    enabled: bool = Field(default=True)
    total_tasks: int = Field(default=0)
    token_created_at: Optional[datetime] = Field(default=None)  # When the token was added (for age tracking)
    rate_limit_state: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Artifact(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="task.id", index=True)

    artifact_type: ArtifactType = Field(sa_column=Column(String, nullable=False))

    # Git-based outputs
    commit_hash: Optional[str] = None
    repo_url: Optional[str] = None
    pr_url: Optional[str] = None

    # For visual feedback or logs
    storage_uri: Optional[str] = None
    logs: Optional[str] = None

    iteration: int = Field(default=0)
    created_at: datetime = Field(default_factory=_utcnow)

    task: Task = Relationship(back_populates="artifacts")
