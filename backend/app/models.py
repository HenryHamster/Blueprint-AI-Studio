from enum import Enum
from typing import Optional, Dict, Any, List
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, String, text
from sqlalchemy.dialects.postgresql import JSONB
import uuid
from datetime import datetime

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

# --- Models ---

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
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class Project(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    user_id: Optional[str] = Field(default=None, index=True)
    status: ProjectStatus = Field(default=ProjectStatus.DRAFT, sa_column=Column(String, default=ProjectStatus.DRAFT))
    github_owner: Optional[str] = None
    github_repo: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Autonomous improvement: user-defined high-level directions for research agents
    directions: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, default=[]))
    # Toggle autonomous research/task generation
    autonomous_enabled: bool = Field(default=True)
    # Self-repo flag: enables rollback monitoring, extended windows, infra soft gates
    # Set True when github_owner/github_repo matches Blueprint's own repo
    is_self_repo: bool = Field(default=False)

    tasks: List["Task"] = Relationship(
        back_populates="project", 
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
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

    created_by: str = Field(default="USER") # "USER", "ORCHESTRATOR", "AGENT_{ID}"
    requires_approval: bool = Field(default=False)
    
    status: TaskStatus = Field(default=TaskStatus.QUEUED, sa_column=Column(String, default=TaskStatus.QUEUED))
    
    # Push Model: Orchestrator assigns this task to a specific VM
    vm_instance_id: Optional[str] = Field(default=None, index=True)
    
    # Task dependency DAG: list of task UUIDs that must be COMPLETED+merged before dispatch
    depends_on: List[str] = Field(default_factory=list, sa_column=Column(JSONB, default=[]))

    # Flexible Payloads: A list of structured dictionaries defining the work.
    # E.g., [{"interface": "ui_component_v1", "data": {...}}]
    payloads: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB))

    # Shared memory/context for the agent during execution
    context: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default=text("'{}'")))

    # Stores parsed plan output from plan-type tasks (for debugging/admin visibility)
    plan_output: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    
    # Agent Heartbeat mechanism
    last_heartbeat: Optional[datetime] = Field(default=None)
    
    attempt_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = Field(default=None)
    archived: bool = Field(default=False)
    
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
    
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    action: str # e.g., "STATUS_CHANGE", "SPLIT", "SPAWNED_CHILD", "MODIFIED_PAYLOAD"
    details: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    
    task: Task = Relationship(back_populates="history")

# Auto-append to status_log whenever latest_status_message is set
from sqlalchemy import event

@event.listens_for(Task.latest_status_message, "set")
def _on_status_message_set(target: Task, value, oldvalue, initiator):
    if value and value != oldvalue:
        entry = {"t": datetime.utcnow().isoformat(), "msg": str(value)[:500]}
        target.status_log = (target.status_log or []) + [entry]


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
    deployed_at: datetime = Field(default_factory=datetime.utcnow)
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


class MaxPlanAccount(SQLModel, table=True):
    """
    Persisted Max plan account for Claude Code OAuth token pooling.
    Each account holds a long-lived OAuth token (sk-ant-oat01-...) generated via `claude setup-token`.
    Active task counts are tracked in-memory only.
    """
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    oauth_token: str  # Long-lived OAuth token from `claude setup-token` (sk-ant-oat01-...)
    claude_config_json: Optional[str] = Field(default=None)  # ~/.claude.json content for onboarding bypass
    enabled: bool = Field(default=True)
    total_tasks: int = Field(default=0)
    token_created_at: Optional[datetime] = Field(default=None)  # When the token was added (for age tracking)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Artifact(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="task.id")
    
    artifact_type: ArtifactType = Field(sa_column=Column(String, nullable=False))
    
    # Git-based outputs
    commit_hash: Optional[str] = None
    repo_url: Optional[str] = None
    pr_url: Optional[str] = None
    
    # For visual feedback or logs
    storage_uri: Optional[str] = None
    logs: Optional[str] = None
    
    iteration: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    task: Task = Relationship(back_populates="artifacts")
