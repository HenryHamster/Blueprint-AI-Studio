"""Bounded HTTP contracts exposed to Strategic Resident workers."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.strategic_resident_turn_results import ResidentEvidenceInspection


class ResidentTurnCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    project_id: uuid.UUID
    authority_generation: int = Field(ge=0)
    reconciliation_id: str = Field(min_length=1, max_length=200)
    expected_turn_task_version: int = Field(ge=0)
    expected_checkpoint_id: uuid.UUID
    expected_checkpoint_version: int = Field(ge=1)
    expected_checkpoint_hash: str = Field(min_length=64, max_length=64)
    context_hash: str = Field(min_length=64, max_length=64)
    result: dict[str, Any]
    accepted_memory_proposal_ids: list[str] = Field(
        default_factory=list, max_length=50
    )
    accepted_implication_ids: list[str] = Field(
        default_factory=list, max_length=50
    )
    accepted_open_loop_ids: list[str] = Field(
        default_factory=list, max_length=50
    )
    resolved_open_loop_ids: list[str] = Field(
        default_factory=list, max_length=50
    )
    evidence_inspections: list[ResidentEvidenceInspection] = Field(
        default_factory=list, max_length=50
    )


class ResidentTurnFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    project_id: uuid.UUID
    failure_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=5_000)
    failure_category: Literal[
        "clone_failed",
        "build_failed",
        "test_failed",
        "lint_failed",
        "pr_creation_failed",
        "claude_code_failed",
        "parse_failed",
        "fatal",
        "rate_limited",
        "no_progress_timeout",
        "timeout",
        "oauth_rotation_failed",
        "reset_failed",
        "reassigned",
        "external_api",
        "vm_config_missing",
        "github_token_fetch_failed",
        "runtime_unavailable",
    ] = "fatal"


__all__ = ["ResidentTurnCompleteRequest", "ResidentTurnFailRequest"]
