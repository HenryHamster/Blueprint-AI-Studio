"""
Pydantic-based payload validation for all supported task types.

Provides:
  - TaskType enum listing all supported handler types
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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.task_capabilities import ALL_TASK_TYPES, TaskType
from app.viewport_evidence import VIEWPORT_WIDTH_MAX, VIEWPORT_WIDTH_MIN

logger = logging.getLogger(__name__)

# Pydantic v2 is required. V1 idioms (@validator, @root_validator, .dict(),
# class Config, parse_obj) are banned — enforced by
# test_no_pydantic_v1_idioms_in_app_source in test_dependency_deprecations.py.
_PYDANTIC_V2_ONLY = True

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

    base_branch: str | None = Field(default=None)
    model: str | None = None
    effort: str | None = Field(default=None, pattern=r"^(low|medium|high|max)$")
    prompt_suffix: str | None = None
    title: str | None = None
    requires_browser: bool = Field(
        default=False,
        description=(
            "Route this task only to execution environments that explicitly prove "
            "browser automation support. Local runners with absent or false proof "
            "are ineligible and the task falls through to cloud dispatch."
        ),
    )

    @field_validator("base_branch")
    @classmethod
    def _check_base_branch(cls, v: str | None) -> str | None:
        return _validate_branch_name(v)


# ---------------------------------------------------------------------------
# Acceptance evidence — the general declaration contract
# ---------------------------------------------------------------------------


# Normalized placeholder tokens that an always-true / trivial declaration tends
# to use. Compared after lower-casing and whitespace-collapsing, so "Assert
# True" and "assert  true" both match. Kept deliberately small and literal — the
# point is to reject obviously-gameable boilerplate, not to police phrasing.
_TRIVIAL_EVIDENCE_TOKENS: frozenset[str] = frozenset(
    {
        "test",
        "test_true",
        "test_truth",
        "test_always_true",
        "always_true",
        "assert true",
        "assert_true",
        "asserttrue",
        "assert true == true",
        "true",
        "pass",
        "test_pass",
        "noop",
        "test_noop",
        "placeholder",
        "test_placeholder",
        "dummy",
        "test_dummy",
        "todo",
        "tbd",
        "n/a",
        "na",
        "none",
    }
)

# Directory components that mark a path as living in a test tree, and a basename
# pattern matching the repo's test-file naming conventions across the languages
# in this codebase (pytest, vitest/jest, go, ruby, elixir).
_TEST_DIR_SEGMENTS: frozenset[str] = frozenset(
    {"tests", "testing", "test", "__tests__", "__test__", "spec"}
)
_TEST_FILE_BASENAME_RE = re.compile(
    r"""^(?:
        test_.*\.py |          # pytest:   test_foo.py
        .*_test\.py |          # pytest:   foo_test.py
        .*\.test\.[cm]?[jt]sx? |  # vitest/jest: foo.test.ts(x)/.mts
        .*\.spec\.[cm]?[jt]sx? |  # jasmine/jest: foo.spec.js(x)
        .*_test\.go |          # go:       foo_test.go
        .*_test\.exs? |        # elixir:   foo_test.exs
        .*_spec\.rb |          # rspec:    foo_spec.rb
        test_.*\.rb            # ruby:     test_foo.rb
    )$""",
    re.VERBOSE,
)


def _normalize_evidence_token(value: str) -> str:
    """Lower-case and collapse whitespace for placeholder comparison."""
    return re.sub(r"\s+", " ", value.strip().lower())


def _is_trivial_evidence(value: str) -> bool:
    """True when *value* is an obviously trivial / always-true placeholder."""
    return _normalize_evidence_token(value) in _TRIVIAL_EVIDENCE_TOKENS


def _looks_like_test_file(path: str) -> bool:
    """True when *path* plausibly names a real test file.

    A path qualifies when it lives under a tests/testing-style directory OR its
    basename matches the repo's test-file naming. Requires the basename to carry
    a file extension so a bare placeholder like ``"test"`` is rejected.
    """
    candidate = path.strip()
    if not candidate:
        return False
    segments = [seg for seg in candidate.replace("\\", "/").split("/") if seg]
    if not segments:
        return False
    basename = segments[-1]
    if "." not in basename:
        return False
    in_test_dir = any(seg.lower() in _TEST_DIR_SEGMENTS for seg in segments[:-1])
    name_matches = bool(_TEST_FILE_BASENAME_RE.match(basename.lower()))
    return in_test_dir or name_matches


def _normalize_plan_acceptance_evidence(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if not set(raw).issubset({"kind", "descriptor", "selector"}):
        return None
    kind = raw.get("kind")
    if kind not in {"test", "behavior", "artifact", "visual"}:
        return None
    descriptor = raw.get("descriptor")
    if not isinstance(descriptor, str):
        return None
    descriptor = descriptor.strip()
    if not descriptor:
        return None
    result: dict[str, Any] = {"kind": kind, "descriptor": descriptor}
    selector = raw.get("selector")
    if selector is not None:
        if not isinstance(selector, str):
            return None
        result["selector"] = selector
    return result


def _split_test_reference(value: str | None) -> tuple[str, str | None] | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    path_part, _, node_part = candidate.partition("::")
    path_part = path_part.strip()
    if not _looks_like_test_file(path_part):
        return None
    node_part = node_part.strip()
    return path_part, node_part or None


def _payload_acceptance_evidence_from_context(raw: Any) -> dict[str, Any] | None:
    evidence = _normalize_plan_acceptance_evidence(raw)
    if evidence is None:
        return None

    kind = evidence["kind"]
    descriptor = evidence["descriptor"]
    selector = evidence.get("selector")
    if kind == "behavior":
        return {"kind": "observable_behavior", "description": descriptor}
    if kind == "artifact":
        return {"kind": "downstream_artifact", "artifact_id": descriptor}
    if kind == "visual":
        if selector is None:
            return None
        try:
            viewport_width = int(selector.strip())
        except ValueError:
            return None
        return {
            "kind": "visual",
            "component": descriptor,
            "viewport_width": viewport_width,
        }
    if kind == "test":
        selector_ref = _split_test_reference(selector)
        if selector_ref is not None:
            path_part, node_part = selector_ref
            result: dict[str, Any] = {"kind": "named_test", "test_path": path_part}
            if node_part:
                result["test_id"] = node_part
            return result

        descriptor_ref = _split_test_reference(descriptor)
        if descriptor_ref is None:
            return None
        path_part, node_part = descriptor_ref
        result = {"kind": "named_test", "test_path": path_part}
        if node_part:
            result["test_id"] = node_part
        elif isinstance(selector, str) and selector.strip():
            result["test_id"] = selector.strip()
        return result
    return None


class AcceptanceEvidence(BaseModel):
    """A small, closed declaration of what would prove a code task's claim true.

    Generalizes :class:`ComputeSuccessCriterion` beyond compute. A task declares
    ONE ``kind`` of forward acceptance evidence at plan time:

      * ``named_test`` — a test that exercises the changed seam end-to-end.
        ``test_path`` must name a plausibly-real test target (under a
        tests/testing directory or matching the repo's test-file naming);
        ``test_id`` optionally pins a node like ``test_fn`` (you may also embed
        it inline as ``path::test_fn`` in ``test_path``).
      * ``observable_behavior`` — a concise ``description`` of a behavior that
        should be exercised.
      * ``downstream_artifact`` — an ``artifact_id`` a later task consumes.
      * ``visual`` — a before/after viewport screenshot pair that proves a
        frontend change looks correct.  ``component`` identifies the UI
        component under test; ``viewport_width`` is the capture width in pixels
        (same bounds as the upload endpoint: 1–4096).  ``assertion_type`` is an
        optional human-readable description of what to look for.

    Deliberately minimal — no expression framework, mirroring
    ``ComputeSuccessCriterion``'s extra-field discipline (``extra='forbid'``).
    This validates only that the DECLARATION is well-formed and substantive; the
    gate-time check that the named test actually touches the CHANGED code is the
    evaluation lane's job, not this schema's. Trivial / always-true declarations
    (empty descriptions, a ``test`` path that is not a test file, placeholders
    like ``test_true`` / ``assert True``) are rejected here, fail-closed, so the
    declaration cannot be gamed at validation time.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["named_test", "observable_behavior", "downstream_artifact", "visual", "coverage_delta"]
    # named_test
    test_path: str | None = None
    test_id: str | None = None
    # observable_behavior
    description: str | None = None
    # downstream_artifact
    artifact_id: str | None = None
    # visual — width bounds mirror the upload endpoint (Field(ge=1, le=4096)).
    # Sourced from the canonical VIEWPORT_WIDTH_* constants in viewport_evidence
    # so this declaration-time bound and the upload-endpoint bound cannot drift.
    component: str | None = None
    viewport_width: int | None = Field(
        default=None, ge=VIEWPORT_WIDTH_MIN, le=VIEWPORT_WIDTH_MAX
    )
    assertion_type: str | None = None
    # coverage_delta — minimum coverage percentage (0-100)
    threshold: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def _coerce_context_shape(cls, value: Any) -> Any:
        return _payload_acceptance_evidence_from_context(value) or value

    @model_validator(mode="after")
    def _check_kind_shape(self) -> AcceptanceEvidence:
        if self.kind == "named_test":
            path_part, _, node_part = (self.test_path or "").partition("::")
            if not _looks_like_test_file(path_part):
                raise ValueError(
                    "named_test acceptance_evidence requires test_path to name a "
                    "real test file (under a tests/testing directory or matching "
                    f"the repo's test-file naming); got {self.test_path!r}"
                )
            # The test reference is the node id — either inline (path::node) or
            # in test_id. It is optional, but if given it must be substantive.
            for ref in (node_part, self.test_id):
                if ref and _is_trivial_evidence(ref):
                    raise ValueError(
                        f"named_test acceptance_evidence has a placeholder test "
                        f"reference {ref!r}; name a real test target"
                    )
        elif self.kind == "observable_behavior":
            description = (self.description or "").strip()
            if len(description) < 10 or _is_trivial_evidence(description):
                raise ValueError(
                    "observable_behavior acceptance_evidence requires a "
                    "substantive description of the behavior to exercise; got "
                    f"{self.description!r}"
                )
        elif self.kind == "downstream_artifact":
            artifact_id = (self.artifact_id or "").strip()
            if not artifact_id or _is_trivial_evidence(artifact_id):
                raise ValueError(
                    "downstream_artifact acceptance_evidence requires a "
                    f"non-trivial artifact_id; got {self.artifact_id!r}"
                )
        elif self.kind == "visual":
            component = (self.component or "").strip()
            if not component or _is_trivial_evidence(component):
                raise ValueError(
                    "visual acceptance_evidence requires a non-trivial component "
                    f"name; got {self.component!r}"
                )
            if self.viewport_width is None:
                raise ValueError(
                    "visual acceptance_evidence requires viewport_width to be set "
                    f"(must be an integer in {VIEWPORT_WIDTH_MIN}–{VIEWPORT_WIDTH_MAX})"
                )
        elif self.kind == "coverage_delta":
            if self.threshold is None:
                raise ValueError(
                    "coverage_delta acceptance_evidence requires a numeric "
                    "threshold (coverage percentage 0–100)"
                )
        return self


class _AcceptanceEvidenceMixin(BaseModel):
    """Mix-in adding the optional, backward-compatible acceptance_evidence field.

    Prepended to each code/PR-producing payload's bases so the declaration stays
    DRY and additive. Optionality + ``exclude_none=True`` on dump means existing
    payloads with no acceptance_evidence validate and round-trip identically.
    """

    acceptance_evidence: AcceptanceEvidence | None = None


def read_acceptance_evidence(first_payload: dict | None) -> AcceptanceEvidence | None:
    """Read a task's declared acceptance_evidence from its ``first_payload``.

    The single accessor downstream subtasks reuse — analogous to how the compute
    ``success_criterion`` is read from the payload. Fail-soft: returns ``None``
    when the field is absent OR the declared shape is malformed, so a consumer
    keeps its default behavior rather than crash on an uninterpretable
    declaration. (The strict, raising validation runs at ingestion via
    ``validate_task_payload``; this read path stays lenient by design.)
    """
    if not isinstance(first_payload, dict):
        return None
    raw = first_payload.get("acceptance_evidence")
    if raw is None:
        return None
    if isinstance(raw, AcceptanceEvidence):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return AcceptanceEvidence.model_validate(raw)
    except Exception:  # noqa: BLE001 — malformed declaration reads as absent
        return None


def _context_acceptance_evidence_from_payload(
    evidence: AcceptanceEvidence,
) -> dict[str, Any] | None:
    if evidence.kind == "named_test":
        test_path = (evidence.test_path or "").strip()
        if not test_path:
            return None
        path_part, _, inline_node = test_path.partition("::")
        result: dict[str, Any] = {
            "kind": "test",
            "descriptor": (evidence.test_id or inline_node or path_part.strip() or test_path).strip(),
            "selector": test_path,
        }
        return result
    if evidence.kind == "observable_behavior":
        description = (evidence.description or "").strip()
        if not description:
            return None
        return {"kind": "behavior", "descriptor": description}
    if evidence.kind == "downstream_artifact":
        artifact_id = (evidence.artifact_id or "").strip()
        if not artifact_id:
            return None
        return {"kind": "artifact", "descriptor": artifact_id}
    if evidence.kind == "visual":
        component = (evidence.component or "").strip()
        if not component or evidence.viewport_width is None:
            return None
        return {
            "kind": "visual",
            "descriptor": component,
            "selector": str(evidence.viewport_width),
        }
    return None


def normalize_acceptance_evidence(
    raw: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize one acceptance-evidence value for both payload and context.

    The payload channel consumes the strict ``AcceptanceEvidence`` vocabulary;
    the context channel consumes the lighter ``PlanAcceptanceEvidence``
    vocabulary. A planner may author either shape. Each side degrades
    independently to ``None`` when it cannot be validly constructed.
    """
    context_evidence = _normalize_plan_acceptance_evidence(raw)
    payload_model: AcceptanceEvidence | None = None
    if isinstance(raw, AcceptanceEvidence):
        payload_model = raw
    elif isinstance(raw, dict):
        try:
            payload_model = AcceptanceEvidence.model_validate(raw)
        except Exception:  # noqa: BLE001 — malformed side reads as absent
            payload_model = None

    payload_evidence = (
        payload_model.model_dump(exclude_none=True) if payload_model is not None else None
    )
    if context_evidence is None and payload_model is not None:
        context_evidence = _context_acceptance_evidence_from_payload(payload_model)
    return payload_evidence, context_evidence


# ---------------------------------------------------------------------------
# Types that require `prompt`
# ---------------------------------------------------------------------------


class _PromptRequired(BasePayload):
    prompt: str = Field(..., min_length=1)


class CreatePayload(_AcceptanceEvidenceMixin, _PromptRequired):
    pass


class RefactorPayload(_AcceptanceEvidenceMixin, _PromptRequired):
    pass


class PlanPayload(_AcceptanceEvidenceMixin, _PromptRequired):
    # A plan task carries the parent finding's declared acceptance evidence so
    # the requirement survives the parent→child decomposition hop. Typed slot
    # (instead of relying on BasePayload's extra='allow' pass-through) so the
    # forward-carried declaration is validated the same as on a code task, and
    # _handle_plan_completion can read it via read_acceptance_evidence. Optional
    # + exclude_none means a plan with no declaration round-trips identically.
    pass


class MigratePayload(_AcceptanceEvidenceMixin, _PromptRequired):
    pass


class InvestigatePayload(_PromptRequired):
    """Operator/strategic-initiated investigation task — just a prompt."""


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


class PanelReviewPayload(ReviewPayload):
    """Read-only PR review payload for the fixed multi-agent panel workflow."""


# ---------------------------------------------------------------------------
# fix — requires prompt + pr_number + branch
# ---------------------------------------------------------------------------


class FixPayload(_AcceptanceEvidenceMixin, BasePayload):
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
    # Aggregated missing-foundation demand (capability/foundation prerequisites
    # declared by dependents that no task provides) computed at spawn time by
    # app.capability_demand.collect_missing_capability_demand. Optional + default
    # None so older payloads without it still validate; injected only when demand
    # is non-empty so the research agent can propose/create the missing foundation
    # or recommend retirement of the stranded demand.
    missing_capability_demand: dict[str, Any] | None = None


class StrategicReviewPayload(_SystemPopulated):
    design_doc: dict[str, Any] | None = None
    vision: dict[str, Any] | None = None
    problem_model: dict[str, Any] | None = None
    running_update: str | None = None
    running_update_acknowledged_at: str | None = None
    user_last_active_at: str | None = None
    self_reflection_log: list[dict[str, Any]] | None = None
    directions_summary: list[dict[str, Any]] | None = None
    outcome_evaluation: dict[str, Any] | None = None
    recent_interventions: dict[str, Any] | None = None
    previous_decision_outcomes: dict[str, Any] | None = None
    open_messages: list[dict[str, Any]] | None = None
    conversation_thread: list[dict[str, Any]] | None = None
    project_name: str | None = None
    research_mode: str | None = None
    is_self_repo: bool | None = None
    enable_self_improvement: bool | None = None
    # Optional, agent-emitted prioritization mode hint. Allowed canonical
    # values: 'audit' | 'fundamentals' | 'ship' | 'pause'. Validated softly:
    # the field is just a string so unknown values still parse, but
    # strategic_completion only persists canonical values to direction
    # records.
    recommended_mode: str | None = None
    max_active_directions: int | None = None
    # Trim-proof authoritative direction counts (active/paused/total/max_active)
    # computed from the full untrimmed direction set before packing, so the agent
    # views anchor on the true active count even when directions_summary is
    # trimmed/dropped under budget. Shipped at critical tier by the packer.
    direction_counts: dict[str, Any] | None = None
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
    # phase_changed_at, tasks_merged_this_version, last_qualification,
    # acceptance_aggregate (additive proven-vs-merged version-window aggregate,
    # carried alongside the raw tasks_merged_this_version count; nullable on
    # legacy rows). Stays ``dict[str, Any] | None`` with extra='allow', so the
    # additive key needs no validation change.
    version_phase: dict[str, Any] | None = None
    # Current integration-branch HEAD CI-compliance signal. Additive compact
    # block: {head_sha, compliant, violation_count, violations, checked,
    # limits_available}. Used by the strategist to spawn cap-bump remediation
    # before asking for qualification.
    ci_compliance: dict[str, Any] | None = None
    # Citable memo summaries (terminal reasoning artifacts spawned from dropped
    # research findings) for this cycle window. Optional + default-None so
    # existing consumers and tests are unaffected. Each entry contains at
    # minimum {task_id, summary_excerpt}; enriched entries also carry optional
    # fields: reasoning_kind, direction_id, tags, citations, success_evidence,
    # retirement_condition, final_metrics, rationale. The reviewer prompt can
    # cite memos by task_id. Summarizer trims heavy fields (citations,
    # final_metrics, rationale) before capping entry count to keep the feed
    # small but citable under budget pressure.
    memo_citations: list[dict[str, Any]] | None = None
    # Per-axis efficiency snapshot with drift vs baseline. Keys: axes (dict of
    # axis_name → {current, baseline, drift_pct, history_tail}),
    # baseline_captured_at, last_snapshot_at. Optional; absent when no snapshot
    # data exists yet (cold-start projects).
    efficiency_snapshot: dict[str, Any] | None = None
    # Held directions: read-only planning context for directions that hit the
    # consecutive-failure hold threshold. Each entry: {direction_id,
    # direction_text, consecutive_failures, last_failure_class,
    # dominant_error_fingerprint, recent_failure_messages}.
    held_directions: list[dict[str, Any]] | None = None
    # Aggregated missing-foundation demand (see ResearchPayload) so the strategist
    # can open a foundation direction or record an explicit retirement. Optional +
    # default None; injected only when demand is non-empty.
    missing_capability_demand: dict[str, Any] | None = None
    # Live local-runner availability (ever_registered, active_total, active_gpu,
    # require_local_runner). Surfaced only when the project has ever registered a
    # runner OR declares the enforce-local policy. Optional + default None so
    # existing payloads without it still validate identically.
    local_runner_status: dict[str, Any] | None = None
    # Trim-proof count of unresolved open operator messages, shipped at critical
    # tier. Computed from the fetch window (_OPEN_MESSAGES_KEEP=40), so it may
    # under-report when the live inbox exceeds that window — but it is always
    # >= what byte-packing surfaces, so the agent can reliably distinguish a
    # dropped inbox from a genuinely empty one and knows to ./fetch messages.
    open_message_count: int | None = None
    # Open FOUND leads from the resident session's examination ledger: surfaces
    # keyed by the latest FOUND verdict that has not been superseded by a later
    # CLEAR/advanced/unevidenced on the same surface. Compact shape per entry:
    # {surfaceKey, note, at}. Optional + default None; injected only when the
    # ledger is present and has at least one open FOUND entry, so existing
    # payloads and callers are unaffected.
    resident_exam_leads: list[dict[str, Any]] | None = None


class DesignDocUpdatePayload(_SystemPopulated):
    design_doc: dict[str, Any] | None = None
    vision: dict[str, Any] | None = None
    problem_model: dict[str, Any] | None = None
    open_messages: list[dict[str, Any]] | None = None
    project_name: str | None = None
    proposed_changes: list[dict[str, Any]] | None = None
    shipped_changes_manifest: list[dict[str, Any]] | None = None
    # Aggregated missing-foundation demand (see ResearchPayload) so the strategist
    # can open a foundation direction or record an explicit retirement. Optional +
    # default None; injected only when demand is non-empty.
    missing_capability_demand: dict[str, Any] | None = None


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


class ReleaseAuditChallengePayload(_SystemPopulated):
    """Payload for the binding release-audit challenger pass.

    Like :class:`ReleaseAuditPayload` the task scores ONE pinned candidate
    commit (``candidate_sha``) against ``base_branch``. It may additionally
    receive primary findings to re-judge when the caller already has them.
    Fields remain optional here because BasePayload is extra-field tolerant
    (``extra='allow'``) and the agent handler fails loud at runtime when the
    candidate SHA is missing/invalid — the schema does not need to re-enforce it.
    """

    diff_range: str | None = None
    prompt: str | None = None
    candidate_sha: str | None = None
    primary_audit_report: dict | None = None


class ReleaseMergeGradePayload(_SystemPopulated):
    """Payload for the blind merge-grade goal-coverage grader (dir d-c0fe439f).

    The agent grades the MERGED DIFFS of one release version (the pinned
    ``candidate_sha`` / ``diff_range`` plus the ``merged_prs`` window)
    against the frozen ``version_goal`` and emits a goal-coverage verdict.  The
    grade is BLIND to the strategist ``running_update`` — that field is
    deliberately absent from this schema, and the spawner guards against it
    being threaded in.  Fields stay optional (BasePayload is extra-tolerant) and
    the handler fails loud at runtime when the candidate SHA / goal is missing.
    """

    diff_range: str | None = None
    candidate_sha: str | None = None
    version_goal: str | None = None
    merged_prs: list[int] | None = None
    prompt: str | None = None


class QAPayload(_AcceptanceEvidenceMixin, _SystemPopulated):
    pass


class DepsPayload(_AcceptanceEvidenceMixin, _SystemPopulated):
    pass


class DocsPayload(_AcceptanceEvidenceMixin, _SystemPopulated):
    pass


class SmokestestPayload(_SystemPopulated):
    health_check_urls: list[str] | None = None


class VerifyTestsPayload(_SystemPopulated):
    """Payload for the verify_tests merge-gate task.

    Like other pipeline tasks it carries the PR head ``branch`` plus
    ``base_branch`` (validated on BasePayload). ``pr_number`` is optional so the
    backend can correlate the verdict artifact with the PR being gated.
    """

    branch: str | None = None
    pr_number: int | None = None

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, v: str | None) -> str | None:
        return _validate_branch_name(v)


class DebugPayload(_SystemPopulated):
    failed_task_id: str | None = None
    failed_task_title: str | None = None
    error_logs: str | None = None
    failed_task_prompt: str | None = None
    failed_task_type: str | None = None


class PerfPayload(_SystemPopulated):
    prompt: str | None = None
    max_severity: Literal["low", "medium", "high", "critical"] | None = None


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


class ComputeSuccessCriterion(BaseModel):
    """A cheap, machine-checkable acceptance criterion for a compute_experiment.

    Two kinds are supported:

    ``kind="metric"`` (default — backward-compatible): a numeric threshold check.
    Declares a ``metric`` name, a comparison ``operator``, and a numeric
    ``threshold``. Make the metric available in ONE of two ways — the success
    criterion reads exactly that value:

      1. write a JSON object ``{metric: value}`` to the file path in the
         ``$BLUEPRINT_RESULT_FILE`` environment variable, or
      2. print a JSON object containing the metric to stdout.

    One-line example for a command declaring ``metric="accuracy"``::

        python eval.py && echo "{\\"accuracy\\": 0.93}" > "$BLUEPRINT_RESULT_FILE"

    (or simply ``print(json.dumps({"accuracy": 0.93}))`` — stdout works too). An
    explicit ``$BLUEPRINT_RESULT_FILE`` write always wins when both are present.
    The metric may also name a built-in run field (``final_cost_usd``,
    ``durable_file_count``, …). A declared metric for which the experiment
    produces NO numeric value does not quietly pass: the run terminates as a
    measurable FAILURE (``compute_unmeasured_criterion``) so "the job ran" can
    never masquerade as "the hypothesis was measured".

    ``kind="artifact_produced"``: artifact-existence check. The criterion is met
    when the job produced at least ``min_file_count`` durable artifact files. Use
    this when the experiment's value IS its durable output (a trained model,
    dataset, checkpoint) and a numeric scalar does not express success. The gate
    checks ``durable_file_count >= min_file_count`` at completion; the artifact
    bundle is always preserved regardless of this criterion.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["metric", "artifact_produced"] = "metric"

    # kind="metric" fields (required when kind="metric"):
    metric: str | None = None
    operator: Literal[">=", "<=", ">", "<", "==", "!="] | None = None
    threshold: float | None = None

    # kind="artifact_produced" field (ignored for kind="metric"):
    min_file_count: int = 1

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "ComputeSuccessCriterion":
        if self.kind == "metric":
            if not isinstance(self.metric, str) or not self.metric.strip():
                raise ValueError("metric is required and non-empty when kind='metric'")
            if self.operator is None:
                raise ValueError("operator is required when kind='metric'")
            if self.threshold is None:
                raise ValueError("threshold is required when kind='metric'")
        return self


class PlanAcceptanceEvidence(BaseModel):
    """An optional, structured proof descriptor that travels on the task CONTEXT.

    Distinct from the payload-borne :class:`AcceptanceEvidence` (which the merge
    gate consumes): this lighter ``{kind, descriptor[, selector]}`` shape is the
    human-readable proof the agent renders in its prompt and best-effort runs —
    see :func:`parse_acceptance_evidence` and the agent's acceptance-test probe.

    Closed vocabulary aligned with the finding ``claims``/``verification_basis``
    fields and the direction-level ``success_evidence`` concept (do NOT invent a
    parallel vocabulary):
      * ``kind="test"`` — a named test or test-selector that exercises the seam
        end-to-end.
      * ``kind="behavior"`` — an observable runtime behavior.
      * ``kind="artifact"`` — an artifact a downstream task consumes.
      * ``kind="visual"`` — a before/after viewport screenshot pair (descriptor
        = component name, selector = viewport width as a string).

    Purely additive: it establishes/propagates/records a contract and never
    blocks a merge. ``extra='forbid'`` (mirroring :class:`ComputeSuccessCriterion`)
    so a malformed declaration — a typo'd kind or an extra key — is rejected by
    :func:`parse_acceptance_evidence` rather than silently carried.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["test", "behavior", "artifact", "visual"]
    descriptor: str
    selector: str | None = None


def _normalize_payload_acceptance_evidence(raw: dict) -> dict | None:
    """Normalize a *payload*-vocabulary acceptance-evidence dict into the lighter
    ``{kind, descriptor[, selector]}`` context shape.

    Findings and plan subtasks declare acceptance_evidence in the payload vocab
    (:class:`AcceptanceEvidence`: ``named_test`` / ``observable_behavior`` /
    ``downstream_artifact`` / ``visual``) — that is the shape the merge gate
    (:func:`read_acceptance_evidence`) consumes. The context channel wants the
    lighter :class:`PlanAcceptanceEvidence` vocab. Historically the two were
    disjoint, so a finding that declared the payload vocab was silently dropped
    from the child context. This maps one onto the other so a single declaration
    populates BOTH channels. Returns ``None`` (never raises) when *raw* is not a
    well-formed payload-vocab declaration, leaving the caller's fall-through to
    treat it as absent.
    """
    try:
        evidence = AcceptanceEvidence.model_validate(raw)
    except Exception:  # noqa: BLE001 — not a payload-vocab declaration → treat as absent
        return None
    if evidence.kind == "named_test":
        # Carry the full path (optionally ``path::node``) as the selector and a
        # human-readable reference as the descriptor.
        selector = (evidence.test_path or "").strip()
        _, _, node = selector.partition("::")
        descriptor = (evidence.test_id or node or selector).strip()
        return {"kind": "test", "descriptor": descriptor, "selector": selector}
    if evidence.kind == "observable_behavior":
        return {"kind": "behavior", "descriptor": (evidence.description or "").strip()}
    if evidence.kind == "downstream_artifact":
        return {"kind": "artifact", "descriptor": (evidence.artifact_id or "").strip()}
    if evidence.kind == "visual":
        result: dict[str, Any] = {
            "kind": "visual",
            "descriptor": (evidence.component or "").strip(),
        }
        if evidence.viewport_width is not None:
            result["selector"] = str(evidence.viewport_width)
        return result
    return None


def parse_acceptance_evidence(raw: Any) -> dict | None:
    """Validate an arbitrary raw acceptance-evidence value into a normalized dict.

    The single source of truth for the acceptance-evidence vocabulary. Returns a
    plain dict (``{kind, descriptor[, selector]}``) when *raw* is a well-formed
    declaration, or ``None`` when it is absent or malformed (missing/bad kind,
    empty descriptor, extra keys, wrong type). NEVER raises — it mirrors the
    defensive read in ``task_evaluators._declared_success_criterion`` so an
    invalid declaration degrades to today's exact behavior rather than failing a
    decomposition or summary path.

    Accepts BOTH historical shapes: the lighter context vocab
    (:class:`PlanAcceptanceEvidence`: ``test`` / ``behavior`` / ``artifact`` /
    ``visual`` with ``descriptor`` / ``selector``) round-trips unchanged, and the
    payload vocab (:class:`AcceptanceEvidence`: ``named_test`` / … the shape the
    merge gate consumes) is normalized into the lighter form. The two vocabularies
    are disjoint on ``kind``, so a declaration matches at most one — previously a
    payload-vocab declaration was silently dropped from the context channel.
    """
    if not isinstance(raw, dict):
        return None
    try:
        evidence = PlanAcceptanceEvidence.model_validate(raw)
    except Exception:  # noqa: BLE001 — fall through to payload-vocabulary bridge
        _payload_evidence, context_evidence = normalize_acceptance_evidence(raw)
        return context_evidence
    descriptor = evidence.descriptor.strip()
    if not descriptor:
        return None
    result: dict[str, Any] = {"kind": evidence.kind, "descriptor": descriptor}
    if evidence.selector is not None:
        result["selector"] = evidence.selector
    return result


class ComputeExperimentPayload(_AcceptanceEvidenceMixin, _SystemPopulated):
    """Reference compute-heavy task (benchmark/evaluation style).

    The agent forwards the launch fields below to ``POST /api/agent/compute-jobs``
    and then only monitors the job to terminal state — it never holds the heavy
    VM. The shape is intentionally minimal and inherits ``extra='allow'`` from
    BasePayload, so new benchmark/evaluation fields can be added without a schema
    bump (backward-compatible extra-field tolerance).

    The ``command`` runs on the compute VM inside a fresh, shallow clone of the
    project repo at the current working directory, with the repo root on
    PYTHONPATH (see ``compute_jobs._attach_repo_clone`` /
    ``compute_backends._build_gcp_startup_script``). Commands import the project's
    packages directly (``from pkg ...`` / ``python3 -m pkg``); they must not rely
    on cloning the repo themselves or on writes persisting past teardown.

    All fields are optional + system-populated; the *agent* handler validates the
    launch requirements (command + a positive spend cap + a positive deadline)
    and fails loudly on misconfiguration rather than silently spending. The
    backend launch endpoint re-validates and clamps cap/deadline to safe bounds.
    """

    # Launch spec — forwarded to the compute-job launch endpoint.
    command: str | None = None
    machine_type: str | None = None
    spend_cap_usd: float | None = None
    deadline_seconds: int | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    backend_name: str | None = None
    provisioning_model: str | None = None
    # Optional live-release proof marker. When present/truthy, compute-job launch
    # is held until the latest promoted release has aggregate required deploy
    # artifacts serving_confirmed; ordinary compute omits this and is unchanged.
    live_proof: bool | str | dict[str, Any] | None = None
    live_proof_marker: str | None = None
    # Optional single-GPU accelerator (canonical short name "T4"/"L4"/"V100").
    # Absent ⇒ a CPU job, unchanged. When set it must be paired with a matching
    # GPU machine_type; the backend rejects A100/over-quota and mismatches loudly.
    accelerator: str | None = None
    # Benchmark/evaluation metadata — informational; echoed back into the result
    # artifact so operators can correlate a job with the experiment that ran it.
    experiment: str | None = None
    benchmark: str | None = None
    hypothesis: str | None = None
    metric: str | None = None
    # Optional goal-level acceptance criterion. When declared, make the metric
    # available by writing {metric: value} JSON to $BLUEPRINT_RESULT_FILE OR by
    # printing a JSON object containing it to stdout; the criterion reads THAT
    # value at completion. A measured-but-missed goal is recorded as honest
    # negative evidence; a declared metric with NO numeric value fails loud
    # (compute_unmeasured_criterion). When absent, presence-based behavior holds.
    success_criterion: ComputeSuccessCriterion | None = None
    # Optional strict-delivery gate. When True, completion FAILS unless the job
    # links a COMPUTE_RESULT artifact whose storage URI is durable-class (gs://,
    # s3://, az://, file:). Absent/False preserves existing fail-soft behavior.
    require_durable_artifact: bool | None = None
    # Optional sweep identifier. When set, this experiment is one arm of a
    # multi-arm sweep sharing the same sweep_id within a project. Completion
    # suppresses the per-arm disposition follow-on and instead fires ONE aggregate
    # follow-on when every sibling arm reaches a terminal state.
    sweep_id: str | None = None
    # Optional setup step the platform runs BEFORE the main command on the compute
    # VM. When present, the startup script executes this as `bash -lc <setup_step>`
    # immediately after the repo clone and before the main command. If the setup
    # step exits non-zero the job fails with reason `setup_step_failed` — no VM
    # budget is consumed by the main command. Use to install dependencies that are
    # absent from the base VM image (e.g. `pip install -r requirements.txt`).
    setup_step: str | None = None
    # Optional no-progress watchdog window (peer of runtime deadline). When set and
    # positive, a job that writes no intermediate progress to BLUEPRINT_PROGRESS_FILE
    # within this many seconds is torn down early with reason `no_progress_timeout`.
    # Absent/None ⇒ inert watchdog; the job runs to its runtime deadline unchanged.
    no_progress_timeout_seconds: int | None = None
    # Optional cross-job checkpoint resume reference. When set to a prior compute
    # job's id, that job's durable artifact bundle is rehydrated into this job's
    # workspace before the command runs (see compute_jobs.create_compute_job /
    # compute_backends._rehydrate_input_artifact_bundle). Absent ⇒ fresh workspace,
    # unchanged. Validated as a UUID string at create time; a missing/foreign/
    # bundle-less reference fails loud rather than silently running empty.
    input_artifact_job_id: str | None = None
    # Optional container image URI. When set the compute backend wraps the command
    # in ``docker run`` with the result-file directory volume-mounted. Absent ⇒
    # existing bare bash -lc path, unchanged. General — no image hardcoding.
    container_image: str | None = None
    # Reproducibility signal — set by the spawn path (plan/research completion)
    # when ``container_image`` resolves. True iff the reference is digest-pinned
    # (``@sha256:``); False for a mutable tag whose layers can drift between runs,
    # silently defeating A/B reproducibility. Absent ⇒ no image declared.
    container_image_digest_pinned: bool | None = None
    # Optional registry credentials for private/gated registries.
    # Shape: {server, username}. The 'password' field is NOT supported in the
    # orchestrated path — there is no delivery channel from create time to the
    # backend launcher. Use a registry accessible via the VM's service-account
    # identity (e.g. Artifact Registry with Workload Identity) or a public image.
    container_registry_auth: dict[str, Any] | None = None
    # NOTE: the legacy ``act_on_verdict`` opt-in (axis B) was retired — disposition
    # is now decided at completion (always-dispose), not by an authoring-time flag.
    # ``extra='allow'`` keeps any legacy payload still carrying the key valid.


class WebResearchPayload(BasePayload):
    """Payload for a web-first research task that produces a knowledge document.

    The task runs read-only with web access and emits a structured document
    (title/summary/body/citations/tags/topic) that the forward-quality gate
    grades before it is persisted as a ``KnowledgeDocument``. ``prompt`` is the
    research question; the remaining fields are optional routing/labeling
    context stamped by the spawner (operator or strategic loop).
    """

    prompt: str | None = None
    topic: str | None = None
    tags: list[str] | None = None
    direction_id: str | None = None
    project_name: str | None = None


class DesignPayload(BasePayload):
    """Payload for a codebase-internal design task that produces a design doc.

    Modeled on :class:`WebResearchPayload`: the task runs read-only over a clone
    of the repo and emits a structured knowledge document
    (title/summary/body/citations/tags/topic) grounded in the actual code. The
    forward-quality gate grades it before it is persisted as a
    ``KnowledgeDocument``. ``prompt`` is the design question; the remaining
    fields are optional routing/labeling context. Extra-field tolerant
    (``extra='allow'`` via BasePayload) per the registry convention.
    """

    prompt: str | None = None
    topic: str | None = None
    tags: list[str] | None = None
    direction_id: str | None = None
    project_name: str | None = None


class RepoBootstrapPayload(BasePayload):
    """Payload for a one-time repo bootstrap task.

    Auto-spawned at project onboarding time for non-self-repo, non-empty
    projects. The agent reads repository structure, confirms build/test
    commands (writing them back via PATCH /api/agent/projects/{id}/build-config/commands),
    and seeds design doc sections before implementation tasks fan out.
    """

    type: Literal["repo_bootstrap"] = "repo_bootstrap"
    github_owner: str = ""
    github_repo: str = ""
    integration_branch: str = ""
    is_first_bootstrap: bool = True


class LaneCheckpointValidated(BaseModel):
    """Validated test/lint state carried by a lane checkpoint."""

    model_config = ConfigDict(extra="ignore")

    lint: bool = True
    tests: str = Field(default="green", pattern=r"^(green|red|skipped)$")


class LaneCheckpointPayload(BaseModel):
    """Lane checkpoint submitted by a completing lane-phase task.

    Validated at task completion; the backend verifies the sha against the
    lane branch via the GitHub client before appending the checkpoint record.
    A missing or unverifiable checkpoint degrades completion to
    ``failure_category='checkpoint_missing'``.
    """

    model_config = ConfigDict(extra="ignore")

    sha: str = Field(min_length=7, max_length=64)
    summary: str = Field(max_length=1024)
    validated: LaneCheckpointValidated = Field(default_factory=LaneCheckpointValidated)
    wip_failing: list[str] = Field(default_factory=list, max_length=50)
    manifest_patch: dict[str, Any] = Field(default_factory=dict)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    vm_cost_usd: float = Field(default=0.0, ge=0.0)
    warm_resume: bool = False

    @field_validator("manifest_patch")
    @classmethod
    def _check_manifest_patch_size(cls, v: dict) -> dict:
        import json as _json

        size = len(_json.dumps(v, default=str).encode())
        if size > 8192:
            raise ValueError(f"manifest_patch exceeds 8KB limit ({size} bytes)")
        return v


class RepoBootstrapOverview(BaseModel):
    """Structured repo-overview payload returned by repo_bootstrap completions."""

    model_config = ConfigDict(extra="ignore")

    tech_stack: str
    directory_layout: str
    first_steps_summary: str
    entry_points: str | None = None
    test_layout: str | None = None
    key_conventions: str | None = None
    environment_requirements: str | None = None
    source_revision: str | None = None


class RepoDomainOverview(BaseModel):
    """Product-domain summary extracted from README/docs during repo_bootstrap.

    Persisted as a KnowledgeDocument with source_kind='repo_domain'.
    All fields are bounded to keep the document lean (total ≤4000 chars).
    """

    model_config = ConfigDict(extra="ignore")

    purpose: str = Field(..., max_length=2000)
    key_concepts: dict[str, str] = Field(default_factory=dict)
    critical_user_flows: list[str] = Field(default_factory=list)
    architectural_gotchas: list[str] = Field(default_factory=list)
    source: str = Field(default="none-found", max_length=500)


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
    TaskType.panel_review: PanelReviewPayload,
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
    TaskType.release_audit_challenge: ReleaseAuditChallengePayload,
    TaskType.release_merge_grade: ReleaseMergeGradePayload,
    TaskType.memo: MemoPayload,
    TaskType.verify_tests: VerifyTestsPayload,
    TaskType.compute_experiment: ComputeExperimentPayload,
    TaskType.web_research: WebResearchPayload,
    TaskType.design: DesignPayload,
    TaskType.repo_bootstrap: RepoBootstrapPayload,
    TaskType.salvage_bundle: BasePayload,
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


# Pre-trim marker key. A section may be shortened *before* the byte-budget
# packer ever runs — e.g. each active gap's append-only ``history`` /
# ``past_moves`` in the strategic ``problem_model`` is trimmed to a recent
# window in ``strategic_spawning._agent_view_problem_model``. That trim never
# flows through the packer's own summarize/drop path, so it would never appear
# in ``_packer_meta`` and the agent would mistake the shortened view for the
# gap's complete arc. ``pack_under_budget`` reflects any such marker found on a
# packed value into ``_packer_meta`` so the agent inspects ONE channel — the
# same one it already reads to decide whether to ``./fetch`` fuller detail.
_PRE_TRIM_MARKER_KEY = "_gap_memory_trimmed"


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

    A schema entry may ALSO declare a ``floor: (value) -> floored_value``
    callback producing a guaranteed-minimum representation. Floored bytes are
    reserved off the top of the budget so a floored key is NEVER starved out
    by other sections — even same- or higher-tier ones that sort ahead of it.
    The floored value always ships (the full/summarized value ships when the
    remaining budget allows); other recoverable context degrades first. This
    is the in-payload delivery guarantee for the strategic ``problem_model``.

    Algorithm: sort by (tier desc, size asc) so within a tier smaller items
    pack first — a 50KB high won't evict a 5KB high purely by insertion
    order. Then greedy-fill: try full size; if over remaining budget call
    ``summarize`` once; if still over, ship the ``floor`` (if any) or drop.
    Critical-tier items that can't fit (even after summarize) raise
    :class:`PayloadTooLargeError` — silently dropping critical context is
    worse than failing loudly.

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

    def _compute_summarized_candidate(part: dict) -> dict[str, Any] | None:
        """Compute and cache the summarized form for *part* once.

        The cached candidate is reused both for degraded-floor reservations and
        for the normal packing path so summarize callbacks are deterministic and
        warnings are emitted at most once per key.
        """
        if part.get("_summarize_computed"):
            return part.get("_summarize_candidate")
        part["_summarize_computed"] = True

        summarize_fn = part.get("summarize")
        if summarize_fn is None:
            part["_summarize_candidate"] = None
            return None

        try:
            trimmed_value = summarize_fn(part["value"])
        except Exception as exc:  # noqa: BLE001 — summarize fns must not crash the packer
            logger.warning(
                "pack_under_budget: summarize for key %r raised %s — dropping",
                part["key"],
                exc,
            )
            part["_summarize_candidate"] = None
            return None

        try:
            trimmed_size = _serialized_size(trimmed_value)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "pack_under_budget: trimmed value for key %r not serializable (%s) — dropping",
                part["key"],
                exc,
            )
            part["_summarize_candidate"] = None
            return None

        size = part["size"]
        assert size is not None
        trimmed_size_int = trimmed_size
        # Defensive: never grow the payload by trimming. If a caller's
        # summarize_fn returns a larger value (caller bug), revert to original
        # and proceed with the original-size check.
        if trimmed_size_int > size:
            logger.warning(
                "pack_under_budget: summarize for key %r grew value (%d > %d) — using original",
                part["key"],
                trimmed_size_int,
                size,
            )
            trimmed_value = part["value"]
            trimmed_size_int = size

        if isinstance(part["value"], list) and isinstance(trimmed_value, list):
            trimmed_meta: dict[str, Any] = {
                "key": part["key"],
                "kind": "list_trimmed",
                "shown": len(trimmed_value),
                "total": len(part["value"]),
            }
        else:
            trimmed_meta = {"key": part["key"], "kind": "summarized"}

        candidate = {
            "value": trimmed_value,
            "size": trimmed_size_int,
            "meta": trimmed_meta,
        }
        part["_summarize_candidate"] = candidate
        return candidate

    # Floor reservations: keys whose schema declares a ``floor`` callback are
    # GUARANTEED to ship at least their floored representation. We reserve that
    # many bytes off the top of the budget so other sections — even same- or
    # higher-tier ones that sort ahead — degrade or drop BEFORE a floored key
    # disappears. This is the in-payload delivery guarantee for the strategic
    # problem_model: a cycle never runs with the model dropped entirely.
    floors: dict[str, dict] = {}
    for p in parts:
        if p["unserializable"]:
            continue
        floor_fn = (schema.get(p["key"]) or {}).get("floor")
        if floor_fn is None:
            continue
        try:
            floored = floor_fn(p["value"])
            floored_size = _serialized_size(floored)
        except Exception as exc:  # noqa: BLE001 — floor fns must not crash the packer
            logger.warning(
                "pack_under_budget: floor for key %r raised %s — no reservation",
                p["key"],
                exc,
            )
            continue
        floors[p["key"]] = {"value": floored, "size": floored_size, "source": "floor"}
    reserved_remaining = sum(f["size"] for f in floors.values())
    # Critical-tier keys without their own floor reservation (e.g. identity /
    # vision) MUST ship at full size — they raise PayloadTooLargeError rather
    # than drop. They are not protected by the reservation mechanism, so they
    # compete against ``budget - reserved_remaining``. If the floor reservations
    # leave no room for them, honoring the floor guarantee would starve a
    # critical key into a hard failure. Account for their full bytes here so we
    # can drop the guarantee BEFORE that happens.
    critical_full = sum(
        p["size"]
        for p in parts
        if p["priority"] == "critical"
        and not p["unserializable"]
        and p["size"] is not None
        and p["key"] not in floors
    )
    # If the full-floor reservations alone — or those reservations plus the
    # non-floored critical keys' full bytes — exceed the budget we cannot honor
    # them all. Before dropping reservations wholesale, try a degraded
    # reservation pass that reserves each floored key's summarized form (when
    # smaller) so same-tier siblings cannot still starve it into a silent drop.
    # This preserves the strategic problem_model's "degrade loudly, don't
    # disappear" contract even in the mixed critical/high contention band.
    if reserved_remaining + critical_full > budget:
        logger.warning(
            "pack_under_budget: floor reservations (%d) + critical keys (%d) "
            "exceed budget (%d) — attempting degraded reservations",
            reserved_remaining,
            critical_full,
            budget,
        )
        degraded_budget = budget - critical_full
        degraded_floors: dict[str, dict] = {}
        degraded_reserved = 0
        if degraded_budget > 0:
            for p in parts:
                if p["key"] not in floors:
                    continue
                # Viable degraded reservations for this key, smallest first. The
                # floor value is ALWAYS available — it is exactly what
                # ``floors[key]`` holds — so it is the guaranteed fallback form.
                # The summarized candidate is added only when usable AND strictly
                # smaller than the full value. Reserving the SMALLEST viable form
                # leaves the most room for the other floored keys under the tight
                # degraded budget; whichever we reserve, the packing loop can
                # still opportunistically ship a richer form if free bytes remain.
                #
                # Falling back to the floor value here is essential: a key may
                # have a valid floor but no usable summarized candidate (no
                # summarize_fn, the summarizer raised, or it was not smaller).
                # Without this fallback such a key would be excluded from
                # ``degraded_floors`` and — if it were the only floored key —
                # ``floors`` would be cleared entirely, dropping a floor that
                # would have fit within ``budget - critical_full`` and breaking
                # the packer's floor guarantee outside the summarize happy path.
                floor_entry = floors[p["key"]]
                degraded_candidates = [
                    {
                        "value": floor_entry["value"],
                        "size": floor_entry["size"],
                        "source": "floor",
                        "meta": {"key": p["key"], "kind": "floored"},
                    }
                ]
                summarized = _compute_summarized_candidate(p)
                if summarized is not None and summarized["size"] < (p["size"] or 0):
                    degraded_candidates.append(
                        {
                            "value": summarized["value"],
                            "size": summarized["size"],
                            "source": "summarized",
                            "meta": summarized["meta"],
                        }
                    )
                best = min(degraded_candidates, key=lambda c: c["size"])
                if degraded_reserved + best["size"] > degraded_budget:
                    continue
                degraded_floors[p["key"]] = {
                    "value": best["value"],
                    "size": best["size"],
                    "source": best["source"],
                    "meta": best["meta"],
                }
                degraded_reserved += best["size"]

        if degraded_floors:
            floors = degraded_floors
            reserved_remaining = degraded_reserved
            logger.warning(
                "pack_under_budget: preserving degraded reservations for keys %s",
                sorted(degraded_floors.keys()),
            )
        else:
            logger.warning(
                "pack_under_budget: no degraded reservations fit — dropping the floor guarantee for this pack"
            )
            floors = {}
            reserved_remaining = 0

    packed: dict = {}
    used = 0
    trimmed: list[dict] = []
    dropped: list[dict] = []
    shipped: list[dict] = []

    for p in parts:
        key = p["key"]
        floor = floors.get(key)
        if floor is not None:
            # Release this key's reservation: those bytes are now claimable by it
            # while the OTHER floored keys' reservations stay protected.
            reserved_remaining -= floor["size"]
        if p["unserializable"]:
            dropped.append(
                {"key": key, "reason": "value not JSON-serializable", "priority": p["priority"]}
            )
            if p["priority"] in ("high", "critical"):
                logger.error(
                    "pack_under_budget: HIGH-TIER section %r (tier=%s) dropped "
                    "(size unknown) — value not JSON-serializable",
                    key, p["priority"],
                )
            else:
                logger.warning("pack_under_budget: dropped %r — value not JSON-serializable", key)
            continue
        size = p["size"]
        assert size is not None
        remaining = budget - used - reserved_remaining
        if size <= remaining:
            packed[key] = p["value"]
            used += size
            shipped.append({"key": key, "bytes": size, "priority": p["priority"]})
            continue
        included = False
        candidate = floors.get(key)
        if (
            candidate is not None
            and candidate.get("source") == "summarized"
            and candidate["size"] <= remaining
        ):
            packed[key] = candidate["value"]
            used += candidate["size"]
            _trim_rec = {**candidate["meta"], "bytes_original": size, "bytes_trimmed": candidate["size"]}
            if _fv := (schema.get(key) or {}).get("fetch_verb"):
                _trim_rec["fetch_verb"] = _fv
            trimmed.append(_trim_rec)
            logger.info(
                "pack_under_budget: trimmed %r (summarized from floor, %d→%d bytes)",
                key, size, candidate["size"],
            )
            included = True
        if not included:
            summarized = _compute_summarized_candidate(p)
            if summarized is not None and summarized["size"] <= remaining:
                packed[key] = summarized["value"]
                used += summarized["size"]
                _trim_rec = {**summarized["meta"], "bytes_original": size, "bytes_trimmed": summarized["size"]}
                if _fv := (schema.get(key) or {}).get("fetch_verb"):
                    _trim_rec["fetch_verb"] = _fv
                trimmed.append(_trim_rec)
                logger.info(
                    "pack_under_budget: trimmed %r (%d→%d bytes)",
                    key, size, summarized["size"],
                )
                included = True
        if not included:
            if floor is not None:
                # Guaranteed-floor fallback: ship the floored value, which is
                # certain to fit within the bytes reserved for this key (just
                # released into ``remaining`` above). Reached only when there is
                # no summarize_fn or it failed/overflowed — the floored value is
                # the last-resort working set the strategist must always receive.
                packed[key] = floor["value"]
                used += floor["size"]
                _trim_rec = {"key": key, "kind": "floored", "bytes_original": size, "bytes_trimmed": floor["size"]}
                if _fv := (schema.get(key) or {}).get("fetch_verb"):
                    _trim_rec["fetch_verb"] = _fv
                trimmed.append(_trim_rec)
                logger.info(
                    "pack_under_budget: trimmed %r to floor (%d→%d bytes)",
                    key, size, floor["size"],
                )
                continue
            if p["priority"] == "critical":
                top_keys = sorted(
                    ((pp["key"], pp["size"] or 0) for pp in parts),
                    key=lambda pair: pair[1],
                    reverse=True,
                )
                raise PayloadTooLargeError(used + (p["size"] or 0), budget, top_keys)
            drop_reason = (
                "exceeds remaining budget even after summarize"
                if p["summarize"]
                else "exceeds remaining budget (no summarize_fn)"
            )
            _drop_rec: dict = {"key": key, "reason": drop_reason, "priority": p["priority"], "bytes": size}
            if _fv := (schema.get(key) or {}).get("fetch_verb"):
                _drop_rec["fetch_verb"] = _fv
            dropped.append(_drop_rec)
            has_floor_in_schema = (schema.get(key) or {}).get("floor") is not None
            if has_floor_in_schema:
                logger.error(
                    "pack_under_budget: CORE section %r dropped (%d bytes) — "
                    "floor guarantee lost; %s",
                    key, size, drop_reason,
                )
            elif p["priority"] in ("high", "critical"):
                # A high/critical-tier section dropping wholesale is a degraded
                # payload the operator must see loudly — the 2026-07-04 incident
                # (14 sections, whole open-message inbox) produced no loud signal.
                logger.error(
                    "pack_under_budget: HIGH-TIER section %r (tier=%s) dropped "
                    "(%d bytes) — %s",
                    key, p["priority"], size, drop_reason,
                )
            else:
                logger.warning(
                    "pack_under_budget: dropped %r (%d bytes) — %s",
                    key, size, drop_reason,
                )

    # Surface any pre-trim marker carried on a shipped section into the packer
    # metadata. The packed section still holds its own marker, but reflecting it
    # here gives the agent a single fetch-decision channel even for trims the
    # packer itself never performed (see _PRE_TRIM_MARKER_KEY).
    gap_memory_trimmed: list[dict] = []
    for key, value in packed.items():
        if isinstance(value, dict):
            marker = value.get(_PRE_TRIM_MARKER_KEY)
            if isinstance(marker, list) and marker:
                gap_memory_trimmed.append({"key": key, "gaps": marker})

    packed["_packer_meta"] = {
        "budget_total": budget,
        "budget_used": used,
        "trimmed": trimmed,
        "dropped": dropped,
        "shipped": shipped,
        "gap_memory_trimmed": gap_memory_trimmed,
    }
    return packed


def validate_task_payload(raw: dict) -> dict:
    """Validate *raw* against the per-type schema.

    Returns a plain dict (model_dump).  Raises ValueError on any violation
    (PayloadTooLargeError — a ValueError subclass — on size overflow).
    Extra keys present in *raw* are preserved (extra='allow').

    For task types registered in the capability registry but absent from the
    payload-model dispatch table, falls back to BasePayload (extra='allow' +
    shared constraints). This enables a new task type to be runnable by
    registration alone, without requiring a parallel edit to the dispatch dict.
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

    model_cls = _MODEL_FOR_TYPE.get(task_type, BasePayload)
    try:
        instance = model_cls.model_validate(raw)
    except Exception as exc:
        raise ValueError(
            f"Invalid payload for task type {type_value!r}: {exc}"
        ) from exc

    result: dict[str, Any] = instance.model_dump(exclude_none=True)
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
