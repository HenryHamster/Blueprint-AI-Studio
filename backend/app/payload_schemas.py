"""
Pydantic-based payload validation for all supported task types.

Provides:
  - TaskType enum listing all 20 handler types
  - ALL_TASK_TYPES frozenset for backward-compat consumers
  - Per-type payload models with required-field enforcement
  - validate_task_payload()  — strict, raises ValueError on bad input
  - validate_internal_payload() — fail-open for schema, fail-closed for size
  - PayloadTooLargeError — raised when serialized payload exceeds the budget
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.task_capabilities import ALL_TASK_TYPES, TaskType

logger = logging.getLogger(__name__)


# Hard upper bound on a serialized task payload. Two constraints stack:
#   1) Agent startup stalls when the claim response is bloated — see the
#      silent pre-claim death investigation (task 595c235d).
#   2) The DB enforces ``octet_length(payloads::text) < 200000`` via
#      ``chk_task_payloads_size`` (alembic 0065). The list wrapper around
#      a single payload adds 2 bytes (``[`` + ``]``), so any caller whose
#      payload passes validation but whose value exceeds 199,997 bytes
#      hits an opaque ``IntegrityError`` at DB insert. The validator and
#      DB cap MUST stay aligned in the safe direction (validator <= DB),
#      otherwise paths that skip ``pack_under_budget`` 500 silently —
#      this regressed strategic-cycle in 2026-05-08 (task 7d6e... fix).
# 190 KB is comfortably under the DB cap, well above any real payload
# (design_doc_update is ~80 KB; pack_under_budget targets 120 KB), and
# leaves ~5 KB of slack for the list-wrapper + UTF-8 multi-byte quirks.
# When raising this, raise the DB constraint in a new migration first.
_MAX_PAYLOAD_BYTES = 190 * 1024


class PayloadTooLargeError(ValueError):
    """Raised when a payload exceeds _MAX_PAYLOAD_BYTES after serialization.

    Inherits from ValueError so existing callers that catch ValueError still
    handle this, but validate_internal_payload re-raises it specifically so
    oversized payloads never queue silently.
    """

    def __init__(self, size: int, limit: int, top_keys: list[tuple[str, int]]):
        self.size = size
        self.limit = limit
        self.top_keys = top_keys
        preview = ", ".join(f"{k}={n}B" for k, n in top_keys[:5])
        super().__init__(
            f"Payload is {size} bytes, exceeds limit of {limit} "
            f"(top keys by size: {preview})"
        )


def _validate_branch_name(v: str | None) -> str | None:
    """Shared branch safety checks — mirrors CreateTaskPayload.validate_branch_name."""
    if v is None:
        return v
    if not v.strip():
        raise ValueError("Branch name cannot be empty")
    if not re.match(r"^[a-zA-Z0-9_./-]+$", v):
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
    model: str | None = None
    effort: str | None = Field(default=None, pattern=r"^(low|medium|high|max)$")
    prompt_suffix: str | None = None
    title: str | None = None

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


class RecentChangesContext(BaseModel):
    """Serialized deploy/git context for investigation payloads.

    Populated at spawn time so the investigation VM (which has no repo
    access) can reference what shipped recently.
    """

    model_config = ConfigDict(extra="allow")

    deployed_at: str | None = None
    current_sha: str | None = None
    commits: list[str] = Field(default_factory=list)
    commit_count: int = 0


class InvestigatePayload(_PromptRequired):
    recent_changes: RecentChangesContext | None = None
    layer_context: dict | None = None


# ---------------------------------------------------------------------------
# review — requires pr_number + branch; prompt optional
# ---------------------------------------------------------------------------


class ReviewPayload(BasePayload):
    pr_number: int = Field(..., gt=0)
    branch: str = Field(..., min_length=1)
    original_prompt: str | None = None
    original_task_id: str | None = None
    original_task_type: str | None = None

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
    original_task_id: str | None = None
    original_prompt: str | None = None

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
    directions: list[dict[str, Any]] | None = None
    research_mode: str | None = None
    current_direction_id: str | None = None
    anomaly_signals: list[dict[str, Any]] | None = None
    project_name: str | None = None
    design_doc: dict[str, Any] | None = None


class StrategicReviewPayload(_SystemPopulated):
    design_doc: dict[str, Any] | None = None
    vision: dict[str, Any] | None = None
    problem_model: dict[str, Any] | None = None
    running_update: str | None = None
    running_update_acknowledged_at: str | None = None
    user_last_active_at: str | None = None
    self_reflection_log: list[dict[str, Any]] | None = None
    is_self_repo: bool | None = None
    directions_summary: list[dict[str, Any]] | None = None
    outcome_evaluation: dict[str, Any] | None = None
    recent_interventions: dict[str, Any] | None = None
    previous_decision_outcomes: dict[str, Any] | None = None
    open_messages: list[dict[str, Any]] | None = None
    conversation_thread: list[dict[str, Any]] | None = None
    project_name: str | None = None
    research_mode: str | None = None
    # Optional, agent-emitted prioritization mode hint. Allowed canonical
    # values: 'audit' | 'fundamentals' | 'ship' | 'pause'. Validated softly:
    # the field is just a string so unknown values still parse, but
    # strategic_completion only persists canonical values to direction
    # records.
    recommended_mode: str | None = None
    max_active_directions: int | None = None
    rejected_proposals: list[dict[str, Any]] | None = None
    anomaly_summary: dict[str, Any] | None = None
    playbook_summary: dict[str, Any] | None = None
    # Per-system maturity snapshots drawn from the SystemMaturity table; lets
    # the strategic agent see audit scores when prioritizing.
    system_maturity: list[dict[str, Any]] | None = None
    # Short-window claim-vs-shipped drift signals; complements lifetime
    # prediction calibration with a fast-reacting drift detector.
    drift_signals: dict[str, Any] | None = None
    # Current LTS version/phase context. Keys: version_number, phase,
    # phase_changed_at, tasks_merged_this_version, last_qualification.
    version_phase: dict[str, Any] | None = None
    # Citable memo summaries (terminal reasoning artifacts spawned from dropped
    # research findings) for this cycle window. Optional + default-None so
    # existing consumers and tests are unaffected. Each entry:
    # {task_id, summary_excerpt}. The reviewer prompt can cite memos by task_id.
    memo_citations: list[dict[str, Any]] | None = None


class DesignDocUpdatePayload(_SystemPopulated):
    design_doc: dict[str, Any] | None = None
    vision: dict[str, Any] | None = None
    problem_model: dict[str, Any] | None = None
    open_messages: list[dict[str, Any]] | None = None
    project_name: str | None = None
    proposed_changes: list[dict[str, Any]] | None = None
    shipped_changes_manifest: list[dict[str, Any]] | None = None


class SecurityAuditPayload(_SystemPopulated):
    prompt: str | None = None


class SelfAuditPayload(_SystemPopulated):
    prompt: str | None = None
    # system_ids to audit this run; None = audit-all with cap
    focus_systems: list[str] | None = None
    # per-run cap for round-robin coverage across all systems
    max_systems: int | None = None


class ReleaseAuditPayload(_SystemPopulated):
    diff_range: str | None = None
    prompt: str | None = None


class QAPayload(_SystemPopulated):
    pass


class DepsPayload(_SystemPopulated):
    pass


class DocsPayload(_SystemPopulated):
    pass


class SmokestestPayload(_SystemPopulated):
    health_check_urls: list[str] | None = None


class DebugPayload(_SystemPopulated):
    failed_task_id: str | None = None
    failed_task_title: str | None = None
    error_logs: str | None = None
    failed_task_prompt: str | None = None
    failed_task_type: str | None = None


class PerfPayload(_SystemPopulated):
    prompt: str | None = None


class MemoPayload(BasePayload):
    """Payload for a terminal reasoning-artifact task (no PR, no child spawn).

    Memo tasks are dispatched when the system wants to record reasoning that
    strategic review can cite later (e.g. a dropped finding worth keeping as
    context, or a strategic note). All fields are optional — the agent reads
    whatever context is available and produces a structured JSON artifact.
    """

    topic: str | None = None
    reasoning_kind: str | None = None
    source_task_id: str | None = None
    source_finding: dict[str, Any] | None = None
    direction_id: str | None = None
    prompt: str | None = None


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
    TaskType.self_audit: SelfAuditPayload,
    TaskType.release_audit: ReleaseAuditPayload,
    TaskType.memo: MemoPayload,
}


def _enforce_payload_size(payload: dict) -> None:
    """Raise PayloadTooLargeError when *payload* would serialize past the budget.

    Runs json.dumps once; callers don't need to re-serialize. The top-key
    byte breakdown attached to the exception makes it obvious which field
    blew the budget (typically one unbounded JSON list).
    """
    size = len(json.dumps(payload, default=str).encode())
    if size <= _MAX_PAYLOAD_BYTES:
        return
    top_keys = sorted(
        ((k, len(json.dumps(v, default=str).encode())) for k, v in payload.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )
    raise PayloadTooLargeError(size, _MAX_PAYLOAD_BYTES, top_keys)


# pack_under_budget priority tiers. Higher tier packs first.
# Use string literals (not raw ints) so call sites read declaratively
# and _packer_meta drop entries describe priority in human terms.
_TIER_RANK: dict[str, int] = {"critical": 1000, "high": 100, "medium": 10, "low": 1}


def _serialized_size(v: Any) -> int:
    """Serialized JSON byte length of *v* using the project-standard `default=str`."""
    return len(json.dumps(v, default=str).encode())


def pack_under_budget(
    payload: dict,
    schema: dict[str, dict],
    budget: int = 120 * 1024,
) -> dict:
    """Pack *payload* keys under *budget* bytes by priority tier.

    Each key is looked up in *schema*: entries should have ``priority`` (one
    of ``"critical"``, ``"high"``, ``"medium"``, ``"low"``) and an optional
    ``summarize: (value) -> trimmed_value`` callback. Missing schema entries
    default to ``priority="low"`` with a logged warning.

    Algorithm: sort by (tier desc, size asc) so within a tier smaller items
    pack first — a 50KB high won't evict a 5KB high purely by insertion
    order. Then greedy-fill: try full size; if over remaining budget call
    ``summarize`` once; if still over, drop. Critical-tier items that can't
    fit (even after summarize) raise :class:`PayloadTooLargeError` —
    silently dropping critical context is worse than failing loudly.

    Returns a new dict containing the packed keys plus a ``_packer_meta``
    entry describing what was trimmed/dropped and the budget accounting.
    The agent prompt is responsible for surfacing this metadata so the
    LLM knows what's available-but-not-shipped (and which fetch endpoint
    to call for it).
    """
    if budget <= 0:
        raise ValueError(f"pack_under_budget: budget must be positive, got {budget}")

    parts: list[dict] = []
    for key, value in payload.items():
        entry = schema.get(key)
        if entry is None:
            logger.warning(
                "pack_under_budget: no schema entry for key %r — defaulting to priority='low'",
                key,
            )
            entry = {}
        priority = entry.get("priority", "low")
        if priority not in _TIER_RANK:
            logger.warning(
                "pack_under_budget: unknown priority %r for key %r — treating as 'low'",
                priority,
                key,
            )
            priority = "low"
        try:
            full_size = _serialized_size(value)
            unserializable = False
        except (TypeError, ValueError) as exc:
            logger.warning(
                "pack_under_budget: key %r not JSON-serializable (%s) — will be dropped",
                key,
                exc,
            )
            full_size = None
            unserializable = True
        parts.append(
            {
                "key": key,
                "value": value,
                "priority": priority,
                "size": full_size,
                "unserializable": unserializable,
                "summarize": entry.get("summarize"),
            }
        )

    parts.sort(
        key=lambda p: (
            -_TIER_RANK[p["priority"]],
            p["size"] if p["size"] is not None else float("inf"),
        )
    )

    packed: dict = {}
    used = 0
    trimmed: list[dict] = []
    dropped: list[dict] = []

    for p in parts:
        key = p["key"]
        if p["unserializable"]:
            dropped.append({"key": key, "reason": "value not JSON-serializable"})
            continue
        remaining = budget - used
        if p["size"] <= remaining:
            packed[key] = p["value"]
            used += p["size"]
            continue
        included = False
        if p["summarize"] is not None:
            try:
                trimmed_value = p["summarize"](p["value"])
            except Exception as exc:  # noqa: BLE001 — summarize fns must not crash the packer
                logger.warning(
                    "pack_under_budget: summarize for key %r raised %s — dropping",
                    key,
                    exc,
                )
            else:
                try:
                    trimmed_size = _serialized_size(trimmed_value)
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "pack_under_budget: trimmed value for key %r not serializable (%s) — dropping",
                        key,
                        exc,
                    )
                    trimmed_size = None
                if trimmed_size is not None:
                    # Defensive: never grow the payload by trimming. If a caller's
                    # summarize_fn returns a larger value (caller bug), revert to
                    # original and proceed with the normal fits/drops decision.
                    if trimmed_size > p["size"]:
                        logger.warning(
                            "pack_under_budget: summarize for key %r grew value (%d > %d) — using original",
                            key,
                            trimmed_size,
                            p["size"],
                        )
                        trimmed_value = p["value"]
                        trimmed_size = p["size"]
                    if trimmed_size <= remaining:
                        packed[key] = trimmed_value
                        used += trimmed_size
                        if isinstance(p["value"], list) and isinstance(
                            trimmed_value, list
                        ):
                            trimmed.append(
                                {
                                    "key": key,
                                    "kind": "list_trimmed",
                                    "shown": len(trimmed_value),
                                    "total": len(p["value"]),
                                }
                            )
                        else:
                            trimmed.append({"key": key, "kind": "summarized"})
                        included = True
        if not included:
            if p["priority"] == "critical":
                top_keys = sorted(
                    ((pp["key"], pp["size"] or 0) for pp in parts),
                    key=lambda pair: pair[1],
                    reverse=True,
                )
                raise PayloadTooLargeError(used + (p["size"] or 0), budget, top_keys)
            dropped.append(
                {
                    "key": key,
                    "reason": (
                        "exceeds remaining budget even after summarize"
                        if p["summarize"]
                        else "exceeds remaining budget (no summarize_fn)"
                    ),
                }
            )

    packed["_packer_meta"] = {
        "budget_total": budget,
        "budget_used": used,
        "trimmed": trimmed,
        "dropped": dropped,
    }
    return packed


def validate_task_payload(raw: dict) -> dict:
    """Validate *raw* against the per-type schema.

    Returns a plain dict (model_dump).  Raises ValueError on any violation
    (PayloadTooLargeError — a ValueError subclass — on size overflow).
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
        ) from None

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
    _enforce_payload_size(result)
    return result


def validate_internal_payload(payload: dict, context: str) -> dict:
    """Validation for internal callers — fail-closed on schema bugs.

    Re-raises:
      * PayloadTooLargeError — oversized payload would stall the agent silently.
      * pydantic.ValidationError (wrapped as ValueError) — required-field or
        constraint violations indicate caller bugs (missing fix_prompt, empty
        prompt, malformed branch). Incident 2026-05-07 (task 8c1ab616): a
        spawn site missed ``fix_prompt`` and the previous fail-open silently
        accepted an empty ``prompt: ""`` payload, burning 3 retries on a
        ``Fatal error: Fix task missing prompt`` agent crash.

    Other exceptions still log a WARNING and return the original payload so a
    truly unexpected error can't take down all spawns at once.
    """
    try:
        return validate_task_payload(payload)
    except (PayloadTooLargeError, ValueError):
        # Schema/size errors are caller bugs, not deployment skew.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "validate_internal_payload failed [%s]: %s — returning original payload",
            context,
            exc,
        )
        return payload
