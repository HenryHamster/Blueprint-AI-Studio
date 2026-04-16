"""Pydantic request/response schemas for Blueprint API endpoints."""
import uuid
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import TaskStatus, ArtifactType


class DeployPayload(BaseModel):
    name: str
    payloads: List[Dict[str, Any]]
    github_owner: Optional[str] = Field(None, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: Optional[str] = Field(None, pattern="^[a-zA-Z0-9_.-]+$")

class AgentStatusPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    status: TaskStatus
    artifact_type: Optional[ArtifactType] = None
    commit_hash: Optional[str] = None
    repo_url: Optional[str] = None
    pr_url: Optional[str] = None
    storage_uri: Optional[str] = None
    logs: Optional[str] = Field(None, max_length=500000)
    verdict_json: Optional[str] = Field(None, max_length=10000)
    failure_category: Optional[str] = Field(None, max_length=50)
    changed_files: Optional[List[str]] = Field(None, max_length=100)
    test_failure_summary: Optional[str] = Field(None, max_length=5000)
    self_review_outcome: Optional[Dict[str, Any]] = None
    diff_summary: Optional[List[Dict[str, Any]]] = Field(None, max_length=50)

class UploadUrlRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    filename: str = Field(..., min_length=1)

class HeartbeatMetrics(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rss_mb: Optional[float] = None
    heap_used_mb: Optional[float] = None
    disk_free_mb: Optional[float] = None
    disk_total_mb: Optional[float] = None
    cgroup_mb: Optional[float] = None
    cgroup_limit_mb: Optional[float] = None

class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    status_message: Optional[str] = None
    metrics: Optional[HeartbeatMetrics] = None

class SpawnTaskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    payloads: List[Dict[str, Any]] = Field(max_length=20)
    requires_approval: bool = False
    context: Optional[Dict[str, Any]] = None
    priority: Optional[int] = Field(default=None, ge=0, le=100)

class GitHubCommitStatusRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    sha: str = Field(..., pattern="^[a-fA-F0-9]{40}$")
    state: str = Field(..., pattern="^(error|failure|pending|success)$")
    description: str = ""
    context: str = "blueprint-agent"
    target_url: str = ""

class GitHubPRCommentRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    pr_number: int
    body: str = Field(..., min_length=1)

class GitHubPRReviewCommentRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    pr_number: int
    commit_id: str = Field(..., pattern="^[a-fA-F0-9]{40}$")
    path: str = Field(..., min_length=1)
    line: int
    body: str = Field(..., min_length=1)

class GitHubSubmitPRReviewRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    pr_number: int
    event: str = Field(..., pattern="^(APPROVE|REQUEST_CHANGES|COMMENT)$")
    body: str = Field(..., min_length=1)
    comments: list[dict] = Field(default_factory=list)

class GitHubCreateIssueRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    body: str = ""
    assignees: Optional[List[str]] = None

class GitHubGetIssuesRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    state: str = Field("open", pattern="^(open|closed|all)$")

class GitHubCreateBranchRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    branch_name: str = Field(..., pattern="^[a-zA-Z0-9_./-]+$")
    sha: str = Field(..., pattern="^[a-fA-F0-9]{40}$")

    @field_validator("branch_name")
    @classmethod
    def validate_branch_safe(cls, v: str) -> str:
        if ".." in v:
            raise ValueError("Branch name cannot contain '..'")
        if v.startswith("/") or v.startswith("-"):
            raise ValueError("Branch name cannot start with '/' or '-'")
        return v

class GitHubCreatePRRequest(BaseModel):
    task_id: uuid.UUID
    owner: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    head: str = Field(..., pattern="^[a-zA-Z0-9_./:-]+$")
    base: str = Field(..., pattern="^[a-zA-Z0-9_./:-]+$")
    body: str = ""


# --- Build Config & Onboarding ---

class BuildConfigDirectoryEntry(BaseModel):
    """A single directory entry within a project's build configuration."""
    path: str = Field(..., min_length=1)
    type: str = Field(..., pattern="^(python|node|go|rust|other)$")
    lint_cmd: Optional[str] = None
    build_cmd: Optional[str] = None
    test_cmd: Optional[str] = None
    install_cmd: Optional[str] = None


class BuildConfig(BaseModel):
    """Schema for project build configuration."""
    language: Optional[str] = Field(None, pattern="^(python|typescript|go|rust|mixed)$")
    directories: List[BuildConfigDirectoryEntry] = Field(default_factory=list)
    env_vars: Dict[str, str] = Field(default_factory=dict)
    git_user_name: str = "Blueprint Agent"
    git_user_email: str = "blueprint-agent@automated.dev"
    detected_at: Optional[str] = None


class OnboardPayload(BaseModel):
    """Payload for the project onboarding endpoint."""
    github_owner: str = Field(..., min_length=1, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: str = Field(..., min_length=1, pattern="^[a-zA-Z0-9_.-]+$")
    name: Optional[str] = None
    build_config_override: Optional[Dict[str, Any]] = None
