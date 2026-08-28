"""Request contracts for operator-launched admin tasks.

Kept outside the large admin router so schema validation remains independently
testable and the router stays within its governed module-size budget.
"""

from typing import Any, Literal
import uuid

from pydantic import BaseModel, Field, field_validator, model_validator

from app.payload_schemas import TaskType


class CreateTaskPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    project_id: uuid.UUID | None = None
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=50000)
    base_branch: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    # Sequenced-task (campaign) fields, mirrored into payloads[0] verbatim.
    # require_deps_landed_on: hold dispatch until every depends_on dependency
    # has LANDED on the named branch (deterministic, heuristic-free gate).
    # release_base_branch: repoint base_branch to the named branch when the
    # dependency gate opens, so the task starts FROM that branch's state
    # including its dependencies' merged work. New tasks may carry depends_on
    # atomically below; legacy/operator edits can still attach cycle-checked
    # edges through PATCH /tasks/{id}.
    require_deps_landed_on: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    release_base_branch: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    # Optional authority attached atomically to the queued task row.  Keeping
    # these fields in the create contract avoids a create-then-PATCH window in
    # which the orchestrator could claim an unscoped or dependency-free task.
    direction_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    criterion_ids: list[str] | None = Field(default=None, max_length=50)
    depends_on: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    task_type: TaskType = Field(default=TaskType.create)
    pr_number: int | None = None
    branch: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$"
    )
    model: str | None = Field(default=None, min_length=1)
    effort: str | None = Field(default=None, pattern=r"^(low|medium|high|max)$")
    # Optional client-supplied idempotency key for deduping retried requests.
    # When provided, the endpoint returns the existing task if one with the same
    # key already exists in an active state, instead of creating a duplicate.
    idempotency_key: str | None = Field(
        default=None, max_length=255, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    # Hotfix path (Phase 5 / automain-LTS): set hotfix=True together with
    # base_branch="main" to mark a plan task as an expedited fix off main.
    # The agent handler branches hotfix/* off main and the orchestrator
    # back-merges main -> automain after the hotfix lands.
    hotfix: bool = False
    # Plan-time acceptance_evidence declaration for code-producing task types
    # (create / fix / refactor / qa / migrate / deps / docs). Required for those
    # types: validate_task_payload rejects a code payload that omits it, so a
    # merged task has a proof the acceptance gate can check and mint criterion
    # credit from. The raw dict is validated strictly against
    # payload_schemas.AcceptanceEvidence at validate_task_payload time.
    acceptance_evidence: dict[str, Any] | None = None
    # Optional originating ProjectMessage (e.g. a BUG_REPORT) this task is being
    # spawned from. Additive and backward compatible - omitted by existing
    # callers. When provided for an ``investigate`` task, the message's citations
    # are revalidated against current HEAD and any "cited code is absent" signal
    # is injected into the spawned task's context + prompt so the investigating
    # agent discounts a stale report. Ignored for all other task types.
    source_message_id: uuid.UUID | None = None
    # Research-only empirical planning envelope. Ordinary task types reject
    # these fields instead of silently carrying research-process boilerplate.
    work_class: Literal["exploratory", "confirmatory", "productization"] | None = None
    stop_or_pivot_condition: str | None = Field(
        default=None, min_length=1, max_length=4000
    )

    @field_validator("work_class", "stop_or_pivot_condition", mode="before")
    @classmethod
    def normalize_empirical_text(cls, v: Any) -> Any:
        if isinstance(v, str):
            normalized = v.strip()
            if not normalized:
                raise ValueError("must not be blank")
            return normalized
        return v

    @field_validator("criterion_ids")
    @classmethod
    def normalize_criterion_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for raw_key in value:
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ValueError("criterion_ids entries must be non-empty strings")
            key = raw_key.strip()
            if len(key) > 64:
                raise ValueError("criterion_ids entries must be at most 64 characters")
            if key in normalized:
                raise ValueError(f"criterion_ids contains duplicate key {key!r}")
            normalized.append(key)
        return normalized

    @field_validator(
        "base_branch", "branch", "require_deps_landed_on", "release_base_branch"
    )
    @classmethod
    def validate_branch_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        for part in v.split("/"):
            if part in (".git", ".lock"):
                raise ValueError(f"Branch name contains reserved component: {part}")
            if part.startswith("."):
                raise ValueError(f"Branch component cannot start with a dot: {part}")
            if ".." in part:
                raise ValueError(f"Branch component cannot contain '..': {part}")
        return v

    @model_validator(mode="after")
    def validate_pr_metadata_required_for_pr_tasks(self) -> "CreateTaskPayload":
        empirical_fields_present = any(
            value is not None
            for value in (
                self.work_class,
                self.stop_or_pivot_condition,
            )
        )
        if empirical_fields_present and self.task_type not in (
            TaskType.research,
            TaskType.web_research,
        ):
            raise ValueError(
                "empirical work contract fields are only supported for research "
                "and web_research tasks"
            )
        if self.task_type in (TaskType.review, TaskType.fix) and self.pr_number is None:
            raise ValueError(
                f"pr_number is required for task_type '{self.task_type.value}'"
            )
        if self.task_type in (TaskType.review, TaskType.fix) and not self.branch:
            raise ValueError(
                f"branch is required for task_type '{self.task_type.value}'"
            )
        return self
