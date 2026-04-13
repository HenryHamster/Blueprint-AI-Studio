"""
Pydantic-based payload validation for all supported task types.

Provides:
  - TaskType enum listing all 17 handler types
  - ALL_TASK_TYPES frozenset for backward-compat consumers
  - Per-type payload models with required-field enforcement
  - validate_task_payload()  — strict, raises ValueError on bad input
  - validate_internal_payload() — fail-open, logs WARNING and returns raw dict
"""
from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


class TaskType(str, Enum):
    create = "create"
    review = "review"
    fix = "fix"
    plan = "plan"
    research = "research"
    strategic_review = "strategic_review"
    design_doc_update = "design_doc_update"
    security_audit = "security_audit"
    qa = "qa"
    deps = "deps"
    docs = "docs"
    migrate = "migrate"
    smoketest = "smoketest"
    refactor = "refactor"
    debug = "debug"
    perf = "perf"
    investigate = "investigate"


# Backward-compat frozenset consumed by existing code
ALL_TASK_TYPES: frozenset[str] = frozenset(t.value for t in TaskType)


def _validate_branch_name(v: Optional[str]) -> Optional[str]:
    """Shared branch safety checks — mirrors CreateTaskPayload.validate_branch_name."""
    if v is None:
        return v
    if not v.strip():
        raise ValueError("Branch name cannot be empty")
    if not re.match(r'^[a-zA-Z0-9_./-]+$', v):
        raise ValueError(f"Branch name contains invalid characters: {v!r}")
    # Reject leading dash (e.g. -bad-branch)
    if v.startswith("-"):
        raise ValueError(f"Branch name cannot start with '-': {v!r}")
    for part in v.split("/"):
        if part in (".git", ".lock"):
            raise ValueError(f"Branch name contains reserved component: {part!r}")
        if part.startswith("."):
            raise ValueError(f"Branch component cannot start with a dot: {part!r}")
        if ".." in part:
            raise ValueError(f"Branch component cannot contain '..': {part!r}")
    return v


class BasePayload(BaseModel):
    """Fields shared by all task types.  extra='allow' so system fields are preserved."""

    model_config = ConfigDict(extra="allow")

    base_branch: str = Field(default="main")
    model: Optional[str] = None
    effort: Optional[str] = Field(default=None, pattern=r"^(low|medium|high|max)$")
    prompt_suffix: Optional[str] = None
    title: Optional[str] = None

    @field_validator("base_branch")
    @classmethod
    def _check_base_branch(cls, v: str) -> str:
        return _validate_branch_name(v)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Types that require `prompt`
# ---------------------------------------------------------------------------

class _PromptRequired(BasePayload):
    prompt: str = Field(..., min_length=1)


class CreatePayload(_PromptRequired):
    pass


class RefactorPayload(_PromptRequired):
    pass


class PlanPayload(_PromptRequired):
    pass


class MigratePayload(_PromptRequired):
    pass


class InvestigatePayload(_PromptRequired):
    pass


# ---------------------------------------------------------------------------
# review — requires pr_number + branch; prompt optional
# ---------------------------------------------------------------------------

class ReviewPayload(BasePayload):
    pr_number: int = Field(..., gt=0)
    branch: str = Field(..., min_length=1)
    original_prompt: Optional[str] = None
    original_task_id: Optional[str] = None
    original_task_type: Optional[str] = None

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, v: str) -> str:
        return _validate_branch_name(v)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# fix — requires prompt + pr_number + branch
# ---------------------------------------------------------------------------

class FixPayload(BasePayload):
    prompt: str = Field(..., min_length=1)
    pr_number: int = Field(..., gt=0)
    branch: str = Field(..., min_length=1)
    original_task_id: Optional[str] = None
    original_prompt: Optional[str] = None

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, v: str) -> str:
        return _validate_branch_name(v)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# System-populated types — no required fields beyond base
# ---------------------------------------------------------------------------

class _SystemPopulated(BasePayload):
    """Backend populates required fields; no extra constraints at ingestion."""


class ResearchPayload(_SystemPopulated):
    directions: Optional[list] = None
    research_mode: Optional[str] = None
    current_direction_id: Optional[str] = None
    anomaly_signals: Optional[list] = None
    project_name: Optional[str] = None
    design_doc: Optional[dict] = None


class StrategicReviewPayload(_SystemPopulated):
    design_doc: Optional[dict] = None
    vision: Optional[dict] = None
    directions_summary: Optional[list] = None
    outcome_evaluation: Optional[dict] = None
    open_messages: Optional[list] = None
    project_name: Optional[str] = None
    research_mode: Optional[str] = None
    max_active_directions: Optional[int] = None


class DesignDocUpdatePayload(_SystemPopulated):
    design_doc: Optional[dict] = None
    vision: Optional[dict] = None
    open_messages: Optional[list] = None
    project_name: Optional[str] = None
    proposed_changes: Optional[list] = None


class SecurityAuditPayload(_SystemPopulated):
    prompt: Optional[str] = None


class QAPayload(_SystemPopulated):
    pass


class DepsPayload(_SystemPopulated):
    pass


class DocsPayload(_SystemPopulated):
    pass


class SmokestestPayload(_SystemPopulated):
    health_check_urls: Optional[list] = None


class DebugPayload(_SystemPopulated):
    failed_task_id: Optional[str] = None
    failed_task_title: Optional[str] = None
    error_logs: Optional[str] = None
    failed_task_prompt: Optional[str] = None
    failed_task_type: Optional[str] = None


class PerfPayload(_SystemPopulated):
    prompt: Optional[str] = None


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_MODEL_FOR_TYPE: dict[TaskType, type[BasePayload]] = {
    TaskType.create: CreatePayload,
    TaskType.refactor: RefactorPayload,
    TaskType.plan: PlanPayload,
    TaskType.migrate: MigratePayload,
    TaskType.investigate: InvestigatePayload,
    TaskType.review: ReviewPayload,
    TaskType.fix: FixPayload,
    TaskType.research: ResearchPayload,
    TaskType.strategic_review: StrategicReviewPayload,
    TaskType.design_doc_update: DesignDocUpdatePayload,
    TaskType.security_audit: SecurityAuditPayload,
    TaskType.qa: QAPayload,
    TaskType.deps: DepsPayload,
    TaskType.docs: DocsPayload,
    TaskType.smoketest: SmokestestPayload,
    TaskType.debug: DebugPayload,
    TaskType.perf: PerfPayload,
}


def validate_task_payload(raw: dict) -> dict:
    """Validate *raw* against the per-type schema.

    Returns a plain dict (model_dump).  Raises ValueError on any violation.
    Extra keys present in *raw* are preserved (extra='allow').
    """
    type_value = raw.get("type")
    if not type_value:
        raise ValueError("Payload is missing required field 'type'")

    try:
        task_type = TaskType(type_value)
    except ValueError:
        valid = ", ".join(sorted(ALL_TASK_TYPES))
        raise ValueError(
            f"Unknown task type {type_value!r}. Valid types: {valid}"
        )

    model_cls = _MODEL_FOR_TYPE[task_type]
    try:
        instance = model_cls.model_validate(raw)
    except Exception as exc:
        raise ValueError(
            f"Invalid payload for task type {type_value!r}: {exc}"
        ) from exc

    result = instance.model_dump(exclude_none=True)
    # Re-inject 'type' — it lives on the raw dict but not on the Pydantic model
    result["type"] = type_value
    return result


def validate_internal_payload(payload: dict, context: str) -> dict:
    """Fail-open validation for internal callers.

    On any validation error, logs a WARNING and returns the original dict
    unchanged so that existing in-flight tasks are never broken.
    """
    try:
        return validate_task_payload(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "validate_internal_payload failed [%s]: %s — returning original payload",
            context,
            exc,
        )
        return payload
