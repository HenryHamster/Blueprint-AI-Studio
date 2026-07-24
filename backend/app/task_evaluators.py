"""Pluggable per-type evaluation gate framework.

Code task types (create/fix/refactor/qa/migrate/deps/docs) get an honest
"was this output any good" answer from the existing two-flag merge gate — a
PR cannot merge until CI is green AND a review signs off. Non-code task types
historically had NO forward quality gate: a memo with unparseable output, or a
perf report that measured nothing, was parsed, stored, and marked COMPLETED on
the agent's say-so.

This module provides the FOUNDATION of a per-type forward gate. A small closed
set of strategies (``none``, ``forward-quality``, ``metric``,
``artifact-delivery``,
``structured-verdict``, plus the ``code-ci-review`` sentinel) each implement a
cheap synchronous ``evaluate`` check, and ``resolve_evaluation_strategy`` picks
the right one by reading the ``evaluation`` field DECLARED on each
``TaskCapability`` in the task-type registry. Selection is therefore
registry-driven — there is no hardwired type→strategy if/elif anywhere.

The ``code-ci-review`` strategy is a recognized sentinel only: code tasks keep
their existing CI + review merge gate and are NEVER routed through this module.
If a code task is ever passed here defensively, the sentinel returns a passing
("delegated") verdict so the framework can never double-gate or block a code
merge.

The heavier independent-grader escalation for findings that spawn downstream
PRs is a separate follow-up; the strategies here are intentionally cheap,
synchronous structural checks appropriate for a terminal artifact.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.acceptance_gate import CRITERION_OPS, coerce_number, criterion_met
from app.json_extract import (
    _extract_json_array,
    _extract_last_json_array,
    scan_balanced_json_objects,
)
from app.models import Task, TaskStatus
from app.shared_state import runtime_config as _runtime_config
from app.task_capabilities import get_task_capability

logger = logging.getLogger(__name__)


# Sentinel strategy key recognized but NOT enforced by this framework — code
# tasks are gated by the existing CI-green + review sign-off merge path.
CODE_CI_REVIEW_KEY = "code-ci-review"

# Context key written on a spawned grader task, pointing at the source task it
# grades. ``task_lifecycle._process_post_commit`` watches for this key to route
# the grader's completion back to the parked source task (the verdict comes back
# asynchronously through the re-entrant completion path, exactly as a code
# review verdict does).
QUALITY_GRADER_SOURCE_KEY = "quality_grader_for_task_id"

# Context key written on the SOURCE task once its grader returns. Presence of
# this key means the artifact has been graded; the source completion handler
# reads it to decide whether to proceed (PASS) or fail (FAIL), and to avoid
# re-spawning a grader on the re-entrant pass.
QUALITY_GRADER_VERDICT_KEY = "quality_grader"

# Context key written on the SOURCE task when the adversarial per-finding grader
# returns. Value is {"finding_verdicts": {hash: bool}, "grader_task_id": "..."}.
# True = finding passed adversarial challenge; False = failed (finding deferred).
# Absent hash → fail-closed (passed=False, explicit=False): only an explicit pass
# from the challenger allows spawning; missing/malformed/non-completed → unspawned.
ADVERSARIAL_GATE_VERDICT_KEY = "adversarial_gate"
ADVERSARIAL_PARSE_OK = "ok"
ADVERSARIAL_PARSE_CHALLENGER_FAILED = "challenger_failed"
ADVERSARIAL_PARSE_EMPTY_LOGS = "empty_logs"
ADVERSARIAL_PARSE_NO_JSON_ARRAY = "no_json_array"
ADVERSARIAL_PARSE_INVALID_JSON = "invalid_json"
ADVERSARIAL_PARSE_NO_HASH_MATCHES = "no_hash_matches"
ADVERSARIAL_WHOLESALE_NON_RESPONSE_OUTCOMES = frozenset(
    {
        ADVERSARIAL_PARSE_CHALLENGER_FAILED,
        ADVERSARIAL_PARSE_EMPTY_LOGS,
        ADVERSARIAL_PARSE_NO_JSON_ARRAY,
        ADVERSARIAL_PARSE_INVALID_JSON,
        ADVERSARIAL_PARSE_NO_HASH_MATCHES,
    }
)


# ---------------------------------------------------------------------------
# Per-finding second-opinion CHALLENGE seam.
#
# Distinct from the batch QUALITY_GRADER above (which grades the WHOLE research
# artifact before any finding spawns work), this seam challenges ONE high-stakes
# per-finding verdict before it is dropped or dispatched: a reproduce-on-HEAD
# 'absent' hard-drop, a low-actionability decay, or (room reserved) an
# indeterminate reproduce verdict that would otherwise dispatch unverified.
#
# The challenge runs as a bounded, read-only ``investigate`` side-car carrying
# FINDING_CHALLENGE_SOURCE_KEY; its completion routes back through the re-entrant
# completion path (task_lifecycle._resolve_finding_challenge_completion) to the
# parked source, which records a FindingChallengeRecord under
# FINDING_CHALLENGE_VERDICT_KEY and re-dispatches its completion handler.
# ---------------------------------------------------------------------------

# The high-stakes per-finding boundaries a challenge can re-examine.
CHALLENGE_BOUNDARY_REPRODUCE_ABSENT = "reproduce_absent"
CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY = "low_actionability_decay"
# Reserved: an indeterminate reproduce verdict that would dispatch unverified.
CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH = "reproduce_indeterminate_dispatch"

_CHALLENGE_BOUNDARIES: frozenset[str] = frozenset(
    {
        CHALLENGE_BOUNDARY_REPRODUCE_ABSENT,
        CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY,
        CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH,
    }
)

# Context key written on a spawned challenge side-car, pointing at the source
# research task whose finding it challenges. ``task_lifecycle._process_post_commit``
# watches for this key (mirroring QUALITY_GRADER_SOURCE_KEY) to route the
# side-car's completion back to the parked source.
FINDING_CHALLENGE_SOURCE_KEY = "finding_challenge_for_task_id"

# Context key written on the SOURCE task once its challenge side-car returns.
# Presence means the challenged finding got its second opinion; the source
# completion handler reads the recorded FindingChallengeRecord to decide whether
# to drop or dispatch the finding, and to avoid re-spawning a challenge.
FINDING_CHALLENGE_VERDICT_KEY = "finding_challenge"

# Context key written on a SOURCE task after a side-car verdict/reconciliation
# has been durably recorded but before the source's completion/gate handler has
# successfully applied it. Recovery sweeps use this to retry application for a
# terminal source that would otherwise look finished.
SIDECAR_VERDICT_APPLICATION_PENDING_KEY = "sidecar_verdict_application_pending"

# Agreement of the second opinion with the first verdict being challenged.
CHALLENGE_AGREES = "agrees"
CHALLENGE_DISAGREES = "disagrees"
CHALLENGE_INDETERMINATE = "indeterminate"

# Schema version stamped on every FindingChallengeRecord.  Increment when the
# record shape changes in a way that would break a consumer expecting the old
# layout.  Version 1 is the first versioned shape; legacy records (missing the
# key) pre-date versioning and should be treated as version 0 by consumers.
FINDING_CHALLENGE_RECORD_SCHEMA_VERSION = 1


def review_is_high_stakes(
    verdict: str,
    issues: list | None,
    investigated_files: list | None,
    review_task: Task,
    *,
    base_branch: str | None,
    trunk_branches: Collection[str],
    diff_non_test_lines: int | None = None,
    diff_non_test_file_count: int | None = None,
    is_observable_behavior: bool = False,
) -> bool:
    """Predicate: does this ordinary ``review`` verdict warrant a second opinion?

    Narrow by design — a panel challenge is a full extra agent run, so it is
    reserved for the one verdict that commits something irreversible: an
    APPROVE on a PR targeting a trunk branch (the project's integration branch
    or promotion target), which hands the PR straight to auto-merge.

    Everything else self-corrects more cheaply without a challenge:

    * a REQUEST_CHANGES feeds the fix loop, and the fixed PR is re-reviewed —
      a wrong rejection costs one fix iteration, not a bad merge;
    * reviews of PRs targeting non-trunk branches (e.g. ``research/*``) are
      re-covered by the rollup review at the trunk boundary.

    Trivially small diffs (non-test lines < ``review_second_opinion_min_lines``
    AND non-test file count <= ``review_second_opinion_min_files``) are also
    exempted — a one-line typo fix does not need a full extra second-opinion run.
    Pass ``diff_non_test_lines`` and ``diff_non_test_file_count`` from the
    source task's ``_pr_diff_summary`` / ``_pr_changed_files`` context; when
    absent the size exemption is skipped (fail-open toward challenging).

    Override: when ``is_observable_behavior`` is True (the reviewed task's
    acceptance evidence is ``observable_behavior`` — the self-reported proof
    path with no machine-checkable assertion), the small-diff exemption is
    skipped and the review is always treated as high-stakes regardless of
    diff size.

    Kept as a single function so operators can re-broaden or further narrow it
    without touching the lifecycle wiring.
    """
    if not (
        verdict == "APPROVE"
        and base_branch is not None
        and base_branch in trunk_branches
    ):
        return False

    # observable_behavior tasks always warrant a second opinion regardless of
    # diff size — they are the only evidence kind that produces
    # satisfied=True, proven=False (entirely self-reported with no
    # machine-checkable assertion). A small diff does not make that weaker
    # proof path more trustworthy.
    if is_observable_behavior:
        return True

    if diff_non_test_lines is not None and diff_non_test_file_count is not None:
        min_lines = int(_runtime_config.get("review_second_opinion_min_lines", 50))
        min_files = int(_runtime_config.get("review_second_opinion_min_files", 4))
        if diff_non_test_lines < min_lines and diff_non_test_file_count <= min_files:
            return False

    return True


@dataclass(frozen=True)
class EvaluationVerdict:
    """Result of running a forward evaluation gate on a terminal artifact.

    ``passed`` False means the output did NOT clear the gate — the caller must
    NOT mark the task COMPLETED clean (park / re-route / fail it instead).
    ``strategy`` is the resolved strategy key (for telemetry and gate-state
    recording). ``delegated`` is True only for the ``code-ci-review`` sentinel,
    where this framework deliberately defers to the existing merge gate.
    """

    passed: bool
    reason: str
    strategy: str

    @property
    def delegated(self) -> bool:
        return self.strategy == CODE_CI_REVIEW_KEY


@runtime_checkable
class EvaluationStrategy(Protocol):
    """A forward evaluation gate. ``key`` matches the registry-declared value."""

    key: str

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        """Return a pass/fail verdict for ``task``'s terminal ``artifact_text``."""
        ...


class NoneStrategy:
    """No forward gate — always passes. For ``plan`` and types with no
    meaningful artifact gate (review, smoketest, verify_tests)."""

    key = "none"

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        return EvaluationVerdict(True, "no forward gate for this task type", self.key)


class CodeCiReviewStrategy:
    """Sentinel for code / PR-producing types. Recognized but never enforced
    here — code tasks keep the existing CI + review merge gate. If a code task
    is routed here defensively we pass-through ("delegated") so the framework
    can never double-gate or block a code merge."""

    key = CODE_CI_REVIEW_KEY

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        return EvaluationVerdict(
            True, "delegated to the existing CI + review merge gate", self.key
        )


class ForwardQualityStrategy:
    """Cheap parse / coherence / structural-validity check for terminal
    knowledge / reasoning / audit artifacts.

    An empty or whitespace-only artifact always fails. For task types with a
    precise structural contract (currently ``memo``) the strategy enforces that
    contract; for the rest it requires the artifact to carry some structured or
    substantive content rather than a bare error token.
    """

    key = "forward-quality"

    # Below this many non-whitespace characters a free-form terminal artifact
    # is treated as too thin to be a coherent reasoning/audit output.
    _MIN_COHERENT_CHARS = 40

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        text = (artifact_text or "").strip()
        if not text:
            return EvaluationVerdict(
                False, "empty terminal artifact — no output produced", self.key
            )

        task_type = _task_type_of(task)

        # Memo has an exact structural contract: a JSON object with summary +
        # reasoning. Reuse the canonical parser so the gate and the handler can
        # never disagree on what "parseable" means. Lazy import avoids a module
        # import cycle (task_completion imports this module).
        if task_type == "memo":
            from app.task_completion import _parse_memo_output

            if _parse_memo_output(artifact_text) is None:
                return EvaluationVerdict(
                    False,
                    "memo output is not a parseable reasoning object "
                    "(missing summary/reasoning)",
                    self.key,
                )
            return EvaluationVerdict(
                True, "memo output parsed to a structured reasoning object", self.key
            )

        # Generic coherence: accept either an embedded JSON structure (the most
        # common shape for findings/verdict artifacts) or a substantive block of
        # prose. A bare one-line error token clears neither bar.
        if _contains_json_structure(text) or len(text) >= self._MIN_COHERENT_CHARS:
            return EvaluationVerdict(
                True,
                "terminal artifact carries structured/substantive content",
                self.key,
            )
        return EvaluationVerdict(
            False, "terminal artifact lacks coherent structured content", self.key
        )


class MetricStrategy:
    """Check that a measurement-producing task actually produced a structured
    measurement. A perf report that produced no parseable measurement fails.

    "No measurement" means no structured result could be extracted from the
    artifact at all — distinct from a measurement that legitimately found zero
    issues (a parseable, possibly empty, result), which passes.

    The metric shape is type-aware (mirroring how ``ForwardQualityStrategy``
    special-cases ``memo``): the perf report is a severity-keyed JSON ARRAY,
    while a ``compute_experiment`` produces a JSON OBJECT carrying the terminal
    job outcome plus a final/accrued cost. The gate fails closed for a compute
    experiment that never reached a terminal job state OR that produced no
    measured cost — exactly the "did it actually produce a terminal, costed
    outcome" check the per-type forward gate exists to enforce. Budget/deadline
    kills are honest terminal outcomes (they carry an ``outcome`` and a cost),
    so they PASS the gate — the experiment did its measured job.

    Compute experiments now declare the separate ``artifact-delivery`` strategy.
    This legacy type-aware branch remains presence-based for defensive callers
    that instantiate MetricStrategy directly.
    """

    key = "metric"

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        task_type = _task_type_of(task)
        if task_type == "compute_experiment":
            return self._evaluate_compute_experiment(task, artifact_text)
        measurement = _extract_measurement(artifact_text)
        if measurement is None:
            return EvaluationVerdict(
                False,
                "metric task produced no parseable structured measurement",
                self.key,
            )
        if task_type == "perf":
            ceiling = (task.first_payload or {}).get("max_severity")
            if isinstance(ceiling, str) and ceiling in _PERF_SEVERITY_RANK:
                ceiling_rank = _PERF_SEVERITY_RANK[ceiling]
                for finding in measurement:
                    if not isinstance(finding, dict):
                        return EvaluationVerdict(
                            False,
                            "malformed perf measurement: element is not a dict",
                            self.key,
                        )
                    sev = str((finding or {}).get("severity") or "").strip().lower()
                    rank = _PERF_SEVERITY_RANK.get(sev)
                    if rank is None:
                        return EvaluationVerdict(
                            False,
                            f"declared perf criterion not met: unrecognized severity {sev!r}",
                            self.key,
                        )
                    if rank > ceiling_rank:
                        return EvaluationVerdict(
                            False,
                            f"declared perf criterion not met: worst severity {sev} exceeds allowed {ceiling}",
                            self.key,
                        )
                return EvaluationVerdict(
                    True,
                    f"structured measurement produced ({len(measurement)} item(s)); "
                    f"declared max severity {ceiling} met",
                    self.key,
                )
        return EvaluationVerdict(
            True,
            f"structured measurement produced ({len(measurement)} item(s))",
            self.key,
        )

    def _evaluate_compute_experiment(
        self, task: Task, artifact_text: str | None
    ) -> EvaluationVerdict:
        result = _extract_compute_measurement(artifact_text)
        if result is None:
            return EvaluationVerdict(
                False,
                "compute experiment produced no terminal, costed job result "
                "(missing terminal outcome or measured final/accrued cost)",
                self.key,
            )
        outcome, cost = result

        criterion = evaluate_compute_success_criterion(task, artifact_text)
        reason = (
            f"compute experiment reached terminal outcome {outcome!r} "
            f"at measured cost ${cost}"
        )
        if criterion is not None:
            status = "met" if criterion["met"] else "recorded as evidence"
            reason += (
                f"; declared success criterion {status} "
                f"({criterion['metric']} {criterion['operator']} "
                f"{criterion['threshold']})"
            )
        return EvaluationVerdict(True, reason, self.key)


class ArtifactDeliveryStrategy:
    """Check that a task delivered its durable artifact/resource.

    For compute_experiment this is intentionally non-gating with respect to the
    declared success criterion: a terminal, costed COMPUTE_RESULT is the durable
    product even when the measured criterion is unmet or absent.
    """

    key = "artifact-delivery"

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        task_type = _task_type_of(task)
        if task_type != "compute_experiment":
            return EvaluationVerdict(
                True,
                "artifact delivery gate has no special contract for this task type",
                self.key,
            )
        result = _extract_compute_measurement(artifact_text)
        if result is None:
            return EvaluationVerdict(
                False,
                "compute experiment delivered no durable terminal result "
                "(missing terminal outcome or measured final/accrued cost)",
                self.key,
            )
        outcome, cost = result
        reason = (
            f"compute experiment delivered terminal result {outcome!r} "
            f"at measured cost ${cost}"
        )
        criterion = evaluate_compute_success_criterion(task, artifact_text)
        if criterion is not None:
            if criterion["met"]:
                reason += (
                    f"; success criterion met ({criterion['metric']} "
                    f"{criterion['operator']} {criterion['threshold']})"
                )
            elif criterion["measurable"]:
                reason += (
                    f"; success criterion unmet ({_format_unmet_criterion(criterion)})"
                )
            else:
                reason += (
                    "; success criterion not reported "
                    f"({criterion['metric']} {criterion['operator']} "
                    f"{criterion['threshold']})"
                )
        return EvaluationVerdict(True, reason, self.key)


class StructuredVerdictStrategy:
    """Strict structural gate for multi-lens verdict artifacts."""

    key = "structured-verdict"

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        text = (artifact_text or "").strip()
        if not text:
            return EvaluationVerdict(
                False,
                "empty terminal artifact — no structured verdict produced",
                self.key,
            )
        return EvaluationVerdict(
            True, "structured verdict parsed with all declared lenses", self.key
        )


# Registry of concrete strategy instances, keyed by their declared strategy key.
# Strategies are stateless, so module-level singletons are safe to share.
_STRATEGIES_SRC: tuple[EvaluationStrategy, ...] = (
    NoneStrategy(),
    CodeCiReviewStrategy(),
    ForwardQualityStrategy(),
    MetricStrategy(),
    ArtifactDeliveryStrategy(),
    StructuredVerdictStrategy(),
)
_STRATEGIES: dict[str, EvaluationStrategy] = {s.key: s for s in _STRATEGIES_SRC}


def resolve_evaluation_strategy(task_type: str | None) -> EvaluationStrategy:
    """Resolve the forward evaluation strategy for ``task_type`` from the registry.

    Reads the ``evaluation`` field DECLARED on the task type's
    ``TaskCapability`` — resolution is registry-driven, never a hardwired
    type→strategy if/elif. Unknown/future types (no capability, or a key not in
    the strategy table) fall back to the non-gating ``none`` strategy so they
    are never silently blocked by a gate they did not opt into.
    """
    capability = get_task_capability(task_type) if task_type is not None else None
    key = capability.evaluation if capability is not None else "none"
    strategy = _STRATEGIES.get(key)
    if strategy is None:
        logger.warning(
            "[EVAL] task_type %r declares unknown evaluation key %r — "
            "falling back to 'none'",
            task_type,
            key,
        )
        return _STRATEGIES["none"]
    return strategy


def evaluate_terminal_output(
    task: Task, artifact_text: str | None
) -> EvaluationVerdict:
    """Resolve ``task``'s strategy from the registry and run the gate on its output."""
    strategy = resolve_evaluation_strategy(_task_type_of(task))
    return strategy.evaluate(task, artifact_text)


def record_failed_gate(task: Task, verdict: EvaluationVerdict) -> None:
    """Mutate ``task`` to reflect a FAILED forward evaluation gate.

    Marks the task FAILED (terminal) rather than COMPLETED clean and records
    the gate verdict under ``context['evaluation_gate']`` for observability.
    The caller is responsible for committing and broadcasting.

    FAILED (not EVALUATING) is deliberate for the terminal non-code handlers
    that use this: they spawn no children and open no PR, so an EVALUATING task
    here has no lifecycle path back out and would hang forever (and pin its
    parent). FAILED is the honest terminal outcome for an artifact that never
    cleared its gate.
    """
    task.status = TaskStatus.FAILED
    task.latest_status_message = (
        f"Evaluation gate ({verdict.strategy}) failed: {verdict.reason}"
    )[:500]
    context: dict[str, Any] = dict(task.context or {})
    context["evaluation_gate"] = {
        "strategy": verdict.strategy,
        "passed": False,
        "reason": verdict.reason,
    }
    task.context = context


def _task_type_of(task: Task) -> str:
    payload = task.first_payload or {}
    value = payload.get("type") if isinstance(payload, dict) else None
    return value or ""


def _contains_json_structure(text: str) -> bool:
    """True if ``text`` contains a balanced JSON object or array somewhere."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                json.loads(candidate)
                return True
            except (json.JSONDecodeError, ValueError):
                continue
    return False


def _extract_measurement(artifact_text: str | None) -> list | None:
    """Extract the structured measurement (a JSON array) from a metric artifact.

    Returns the parsed list, or ``None`` when no structured measurement could be
    extracted at all. Reuses the canonical findings-array extractor so the gate
    agrees with the perf handler on what counts as a parseable measurement.
    """
    text = (artifact_text or "").strip()
    if not text:
        return None
    json_str = _extract_json_array(text, "severity")
    if not json_str:
        return None
    try:
        parsed = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


# Alias the shared fail-closed numeric core (now owned by app.acceptance_gate so
# the compute success-criterion gate and the code acceptance gate share ONE
# core). Existing references in this module keep their underscored names.
_coerce_number = coerce_number


def _extract_compute_measurement(
    artifact_text: str | None,
) -> tuple[str, float] | None:
    """Extract a compute-experiment terminal result from a COMPUTE_RESULT artifact.

    The agent's terminal artifact is a JSON OBJECT (see
    ``agent/src/handlers/compute-experiment.ts``) carrying the terminal job
    ``outcome`` plus ``final_cost_usd``/``accrued_cost_usd``. Returns
    ``(outcome, cost)`` only when the result reached a terminal outcome AND
    carries a measured cost; returns ``None`` (gate fails closed) for an empty,
    unparseable, outcome-less, or cost-less artifact. ``final_cost_usd`` is
    preferred; ``accrued_cost_usd`` is the fallback the agent itself uses.
    """
    obj = _parse_artifact_json(artifact_text)
    if obj is None:
        return None
    outcome = obj.get("outcome")
    if not isinstance(outcome, str) or not outcome.strip():
        return None
    cost = _coerce_number(obj.get("final_cost_usd"))
    if cost is None:
        cost = _coerce_number(obj.get("accrued_cost_usd"))
    if cost is None:
        return None
    return outcome.strip(), cost


# The comparison-operator table is the shared fail-closed core, now owned by
# app.acceptance_gate. Aliased here so the compute success-criterion gate and the
# code acceptance gate compare numbers identically.
_CRITERION_OPS = CRITERION_OPS

# The terminal COMPUTE_RESULT artifact (agent/src/handlers/compute-experiment.ts)
# always carries these numeric, job-level fields. It ALSO now carries any
# command-reported experiment metric (accuracy, loss, throughput, …): the agent
# reads ComputeJob.polling_metadata.result_metrics and flattens each numeric one
# to a top-level key alongside the cost fields. So a success_criterion is
# MEASURABLE whenever its metric is present as a numeric value in the result —
# whether a reserved cost/operational field below OR a reported experiment
# metric. These reserved keys are the always-present job-level fields, listed
# for documentation and so reported metrics never collide with them.
_RESERVED_COMPUTE_RESULT_KEYS: frozenset[str] = frozenset(
    {"final_cost_usd", "accrued_cost_usd", "spend_cap_usd", "poll_attempts"}
)

# Metrics whose values are derived backend-side from the job's captured release
# echo, not from anything the experiment command wrote to its COMPUTE_RESULT.
_RELEASE_ECHO_METRICS: frozenset[str] = frozenset(
    {"release_echo_present", "release_echo_verified"}
)


def derive_release_binding_status(
    echo_sha: str | None, expected_sha: str | None
) -> str:
    """Derive release binding status from the VM echo SHA and the expected promoted SHA.

    Returns ``'verified'`` when the echo matches the expected SHA, ``'absent'``
    when no echo was captured, and ``'mismatched'`` when the echo does not match.
    Exported so knowledge_completion and task_completion share one implementation.
    """
    if echo_sha is None:
        return "absent"
    if echo_sha == expected_sha:
        return "verified"
    return "mismatched"


def _release_echo_numeric(binding_status: str, metric: str) -> float:
    """Convert a release binding status to a numeric 0.0/1.0 for a release-echo metric.

    ``release_echo_present``: 1.0 when the echo was captured (verified or mismatched).
    ``release_echo_verified``: 1.0 only when the echo matches the expected promoted SHA.
    """
    if metric == "release_echo_present":
        return 1.0 if binding_status in ("verified", "mismatched") else 0.0
    return 1.0 if binding_status == "verified" else 0.0


def _declared_success_criterion(task: Task) -> tuple[str, str, float] | None:
    """Read a compute_experiment's declared success criterion from its payload.

    Returns ``(metric, operator, threshold)`` only when the payload carries a
    well-formed ``success_criterion`` (a non-empty metric name, a recognized
    comparison operator, and a numeric threshold). Returns ``None`` when no
    criterion is declared OR the declared shape is malformed — in both cases the
    gate keeps its presence-based behavior rather than fail a task on a
    criterion it cannot interpret.

    Handles two criterion kinds:
      * ``kind="metric"`` (default): reads ``metric``/``operator``/``threshold``
        and returns them directly.
      * ``kind="artifact_produced"``: translates to a ``durable_file_count >= N``
        check so the existing numeric evaluation pipeline applies unchanged.
        ``durable_file_count`` is a backend-derived key always present in the
        COMPUTE_RESULT when a durable artifact exists (from ``_link_terminal_artifact``).
    """
    payload = task.first_payload or {}
    if not isinstance(payload, dict):
        return None
    raw = payload.get("success_criterion")
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("kind") or "metric")
    if kind == "artifact_produced":
        try:
            min_count = int(raw.get("min_file_count") or 1)
        except (TypeError, ValueError):
            min_count = 1
        return ("durable_file_count", ">=", float(max(1, min_count)))

    metric = raw.get("metric")
    op = raw.get("operator")
    threshold = _coerce_number(raw.get("threshold"))
    if not isinstance(metric, str) or not metric.strip():
        return None
    if op not in _CRITERION_OPS or threshold is None:
        return None
    return metric.strip(), op, threshold


def _find_metric_case_insensitive(obj: dict, metric: str) -> object:
    """Find a metric value by case/whitespace-normalized key.

    Returns the matched value, or None when no key matches or multiple
    distinct keys match (ambiguity guard: a collision is more likely a
    schema mistake than an intentional overload; fail closed).
    """
    normalized = metric.strip().lower()
    matches = [(k, v) for k, v in obj.items() if k.strip().lower() == normalized]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "[EVAL] Ambiguous case-insensitive metric match for %r: found keys %r "
            "— failing closed",
            metric,
            [k for k, _ in matches],
        )
        return None
    return matches[0][1]


def _find_metric_in_arms(
    obj: dict, metric: str, op: str
) -> tuple[object, str | None]:
    """Scan the ``arms`` block of a multi-arm COMPUTE_RESULT for ``metric``.

    Returns ``(raw_value, arm_name)`` where ``raw_value`` is the best-arm
    numeric value for the declared ``op`` (max for >= / >; min for <= / <;
    any for == / !=), or ``(None, None)`` when no arm carries a numeric value.

    Handles two arm shapes:
    * dict:  ``{"arms": {arm_name: {metric: value, ...}}}``
    * list:  ``{"arms": [{"name": arm_name, metric: value, ...}]}``

    Non-dict arm entries are skipped gracefully.
    """
    arms_raw = obj.get("arms")
    if arms_raw is None:
        return None, None

    def _get_arm_value(arm_dict: dict) -> object:
        v = arm_dict.get(metric)
        if v is None:
            v = _find_metric_case_insensitive(arm_dict, metric)
        return v

    arm_pairs: list[tuple[str, object]] = []
    if isinstance(arms_raw, dict):
        for arm_name, arm_data in arms_raw.items():
            if not isinstance(arm_data, dict):
                continue
            v = _get_arm_value(arm_data)
            n = _coerce_number(v)
            if n is not None:
                arm_pairs.append((str(arm_name), n))
    elif isinstance(arms_raw, list):
        for item in arms_raw:
            if not isinstance(item, dict):
                continue
            arm_name = str(item.get("name") or "")
            v = _get_arm_value(item)
            n = _coerce_number(v)
            if n is not None:
                arm_pairs.append((arm_name, n))

    if not arm_pairs:
        return None, None

    # Select the best-arm value: for >= / > maximize; for <= / < minimize;
    # for == / != take the first arm that has the metric.
    if op in (">=", ">"):
        best = max(arm_pairs, key=lambda t: t[1])
    elif op in ("<=", "<"):
        best = min(arm_pairs, key=lambda t: t[1])
    else:
        best = arm_pairs[0]

    return best[1], best[0]


def evaluate_compute_success_criterion(
    task: Task,
    artifact_text: str | None,
    *,
    echo_sha: str | None = None,
    expected_sha: str | None = None,
) -> dict | None:
    """Evaluate a compute_experiment's declared success criterion against its result.

    Returns a small operator-readable dict
    ``{metric, operator, threshold, value, met, measurable}`` describing whether
    the reported result met the declared goal, or ``None`` when no criterion is
    declared (callers then fall back to presence-based behavior). ``measurable``
    now means simply "the metric is present as a numeric value in the terminal
    COMPUTE_RESULT" — true for a reserved cost field OR a command-reported
    experiment metric the agent flattened to a top-level key. A declared metric
    that is absent/non-numeric is NOT measurable and is a FAIL-CLOSED outcome
    (``met`` is False), not a free pass. ``value`` is the numeric metric read by
    name out of the terminal result object; ``met`` is False when the metric is
    absent/non-numeric (fail-closed) or when the comparison does not hold.

    For release-echo metrics (``release_echo_present``, ``release_echo_verified``),
    the value is derived backend-side from ``echo_sha`` and ``expected_sha`` rather
    than from the artifact JSON. This means the criterion is always EVALUABLE (never
    absent/unmeasurable) as long as the caller passes the echo data, even when the
    experiment command emitted no COMPUTE_RESULT before timing out.
    """
    criterion = _declared_success_criterion(task)
    if criterion is None:
        return None
    metric, op, threshold = criterion
    binding_status: str | None = None
    best_arm: str | None = None
    per_arm_values: dict | None = None
    if metric in _RELEASE_ECHO_METRICS:
        binding_status = derive_release_binding_status(echo_sha, expected_sha)
        value: float | None = _release_echo_numeric(binding_status, metric)
        raw_value: object = value
    else:
        obj = _parse_artifact_json(artifact_text) or {}
        arms_raw = obj.get("arms")
        if isinstance(arms_raw, (dict, list)):
            # Multi-arm result — aggregate using best-arm semantics.
            # "Best arm" = the arm value most favorable for the declared operator so
            # that met=True whenever at least one arm met the criterion.
            per_arm_values = {}
            if isinstance(arms_raw, dict):
                for arm_name, arm_data in arms_raw.items():
                    if not isinstance(arm_data, dict):
                        continue
                    arm_val = arm_data.get(metric)
                    if arm_val is None:
                        arm_val = _find_metric_case_insensitive(arm_data, metric)
                    per_arm_values[str(arm_name)] = _coerce_number(arm_val)
            else:
                for item in arms_raw:
                    if not isinstance(item, dict):
                        continue
                    arm_name = str(item.get("name") or item.get("label") or item.get("arm") or "")
                    arm_val = item.get(metric)
                    if arm_val is None:
                        arm_val = _find_metric_case_insensitive(item, metric)
                    per_arm_values[arm_name] = _coerce_number(arm_val)
            measurable_vals = [v for v in per_arm_values.values() if v is not None]
            if measurable_vals:
                if op in (">=", ">"):
                    raw_value = max(measurable_vals)
                elif op in ("<=", "<"):
                    raw_value = min(measurable_vals)
                else:
                    raw_value = next(
                        (v for v in measurable_vals if criterion_met(v, op, threshold)),
                        measurable_vals[0],
                    )
                best_arm = next(
                    (name for name, val in per_arm_values.items() if val == raw_value),
                    None,
                )
            else:
                raw_value = None
        else:
            raw_value = obj.get(metric)
            if raw_value is None:
                raw_value = _find_metric_case_insensitive(obj, metric)
        value = _coerce_number(raw_value)
    # Shared fail-closed core: a metric absent/non-numeric in the result is never
    # "met" (and ``criterion_met`` enforces that identically for the code gate).
    met = criterion_met(raw_value, op, threshold)
    result: dict = {
        "metric": metric,
        "operator": op,
        "threshold": threshold,
        "value": value,
        "met": met,
        "measurable": value is not None,
    }
    if best_arm is not None:
        result["best_arm"] = best_arm
    if binding_status is not None:
        result["binding_status"] = binding_status
    if per_arm_values is not None:
        result["per_arm"] = per_arm_values
    return result


def _format_unmet_criterion(criterion: dict) -> str:
    """Operator-readable reason for a compute success criterion that was not met."""
    metric = criterion["metric"]
    op = criterion["operator"]
    threshold = criterion["threshold"]
    value = criterion["value"]
    if value is None:
        # Fail CLOSED: the goal was declared but the experiment reported no
        # numeric value to check it against (the metric is absent or non-numeric
        # in the terminal COMPUTE_RESULT). An undeclared metric never reaches
        # here; an unreported declared metric must NOT pass as "unmeasurable".
        return (
            f"declared success criterion not evaluable: declared "
            f"{metric} {op} {threshold} but the experiment reported no "
            f"numeric {metric!r} in its result"
        )
    return (
        f"declared success criterion not met: {metric}={value} "
        f"(required {op} {threshold})"
    )


_PERF_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _parse_artifact_json(artifact_text: str | None) -> dict | None:
    """Parse the first balanced JSON object from ``artifact_text``, or ``None``.

    Tries a direct parse first (the agent emits a bare ``JSON.stringify``
    object); falls back to scanning for the first balanced ``{...}`` span so a
    result wrapped in surrounding log text is still recovered.
    """
    text = (artifact_text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return scan_balanced_json_objects(text)


# Fields that are always present in a COMPUTE_RESULT but are NOT
# experiment-emitted metric keys (they are operational/job-level fields).
_COMPUTE_RESULT_NON_METRIC_KEYS: frozenset[str] = (
    _RESERVED_COMPUTE_RESULT_KEYS
    | frozenset({"outcome", "status", "job_id", "intentional_terminal"})
)


def describe_emitted_compute_metric_keys(artifact_text: str | None) -> str:
    """Produce a one-line diagnostic of what experiment metric keys the agent emitted.

    Used by the compute_unmeasured_criterion failure path to tell the operator
    what the command actually wrote so a case-mismatch is instantly visible:
    declared 'auc' but emitted 'AUC' shows up as "emitted_keys=['AUC']".

    Two distinguishable cases:
    * no COMPUTE_RESULT parsed at all → experiment likely crashed before writing
      its result JSON (happens when the command fails at startup and emits only
      a traceback with no terminal JSON block).
    * result parsed but declared key absent → command completed, wrote a result,
      but the metric name differs (case mismatch, typo) or was never written.
    """
    obj = _parse_artifact_json(artifact_text)
    if obj is None:
        return "no COMPUTE_RESULT parsed — experiment likely crashed before emitting result"
    emitted = sorted(k for k in obj if k not in _COMPUTE_RESULT_NON_METRIC_KEYS)
    if not emitted:
        return "emitted_keys=[] (command wrote no experiment metrics to COMPUTE_RESULT)"
    return f"emitted_keys={emitted!r} (declared key absent — possible case/name mismatch)"


# ---------------------------------------------------------------------------
# Forward-quality ESCALATION: independent grader for blast-radius artifacts.
#
# The cheap synchronous ``ForwardQualityStrategy`` check above is appropriate
# for a purely TERMINAL artifact (a memo, an audit report) that spawns nothing.
# But a research/knowledge artifact whose findings would SPAWN downstream
# implementation work carries real blast radius: a wrong-but-plausible finding
# can launch a PR. For those, the gate escalates from "is it parseable" to a
# real INDEPENDENT GRADER — the same shape as a code review (a fresh agent, a
# rubric, a structured verdict) applied to an artifact instead of a diff.
#
# The grader is spawned as its own task (a read-only ``investigate`` runner the
# agent already knows how to execute) carrying ``QUALITY_GRADER_SOURCE_KEY`` so
# its completion routes back through the re-entrant completion path to the
# parked source task. The source is parked EVALUATING — it does NOT complete or
# spawn children until the verdict returns, mirroring how a create task parks
# awaiting its review verdict.
# ---------------------------------------------------------------------------


_ADVERSARIAL_MANIFEST_CHAR_BUDGET = 14_000
_ADVERSARIAL_MANIFEST_DESC_CAP = 400


def build_adversarial_challenge_manifest(
    high_stakes: list[dict],
) -> tuple[list[dict], str]:
    """Build a compact, budget-bounded JSON manifest for the adversarial challenger.

    Each item keeps only the fields the challenger needs to judge a finding:
    ``index`` (1-based ordinal for robust binding), ``hash``, ``title``,
    ``description`` (≤400 chars), ``risk``, ``scope``, plus provenance
    sentinels ``verification_basis_downgraded_from`` and ``needs_revalidation``
    when present.

    Items are admitted greedily until the serialised JSON would exceed
    ``_ADVERSARIAL_MANIFEST_CHAR_BUDGET``.  The returned list is always a
    valid JSON array; the second element of the tuple is the serialised string.
    """
    compact: list[dict] = []
    running_json = "[]"
    for item in high_stakes:
        entry: dict = {}
        h = str(item.get("hash") or "")
        if not h:
            continue
        # 1-based index so the challenger can echo it back for robust binding
        # even when it mis-transcribes the hash.
        entry["index"] = len(compact) + 1
        entry["hash"] = h
        entry["title"] = str(item.get("title") or "")[:200]
        desc = str(item.get("description") or item.get("prompt") or "")
        entry["description"] = desc[:_ADVERSARIAL_MANIFEST_DESC_CAP]
        if item.get("risk"):
            entry["risk"] = str(item["risk"])
        if item.get("scope"):
            entry["scope"] = str(item["scope"])
        if item.get("verification_basis_downgraded_from"):
            entry["verification_basis_downgraded_from"] = str(
                item["verification_basis_downgraded_from"]
            )
        if item.get("needs_revalidation"):
            entry["needs_revalidation"] = True
        candidate = [*compact, entry]
        candidate_json = json.dumps(candidate)
        if len(candidate_json) > _ADVERSARIAL_MANIFEST_CHAR_BUDGET:
            break
        compact = candidate
        running_json = candidate_json
    return compact, running_json


def build_adversarial_finding_prompt(high_stakes_findings_json: str) -> str:
    """Build a refute-first per-finding adversarial challenge prompt.

    Instructs a fresh agent to actively try to REFUTE each finding before it
    spawns implementation work. Fail-closed semantics: only an explicit pass
    verdict allows spawning; absent or malformed output leaves findings unspawned.
    """
    return (
        "You are an ADVERSARIAL CHALLENGER. Each of the following research "
        "findings is about to SPAWN implementation work (PRs, plans). Your job "
        "is to actively try to REFUTE each finding — not to confirm it. Default "
        "to skeptical: if you cannot confidently confirm all three criteria, "
        "mark the finding as failing.\n\n"
        "For each finding, judge:\n"
        "  1. PREMISE — Is the finding's core premise still true at HEAD? "
        "(The cited code/behavior exists AND has the claimed problem.)\n"
        "  2. EVIDENCE — Are the citations real and do they actually support "
        "the conclusion, not just tangentially related?\n"
        "  3. SCOPE — Does the finding overstate its blast radius, severity, "
        "or impact beyond what the cited evidence actually shows?\n\n"
        "FAIL when uncertain. Only PASS a finding when all three criteria "
        "clearly hold at HEAD.\n\n"
        "Findings to challenge (JSON array, each has an 'index' and 'hash' "
        "identifier):\n"
        "-----8<----- FINDINGS START -----8<-----\n"
        f"{high_stakes_findings_json}\n"
        "-----8<----- FINDINGS END -----8<-----\n\n"
        "End your response with EXACTLY ONE JSON array, one object per finding "
        "in the SAME ORDER as the input, and nothing after it:\n"
        '[{"index": <1-based index from input>, "hash": "<finding_hash from input>", '
        '"verdict": "pass" | "fail", '
        '"reason": "<one-sentence justification>"}]'
    )


def parse_adversarial_finding_verdicts(
    logs: str | None,
    finding_hashes: list[str],
    completed: bool = True,
) -> tuple[dict[str, dict], str]:
    """Parse per-finding pass/fail verdicts from adversarial grader output.

    Returns ``(verdicts, parse_outcome)`` where ``verdicts`` maps
    finding_hash → {passed, reason, explicit, unresolved_reason?} and
    ``parse_outcome`` is a compact machine-readable classification of the
    parse pipeline's highest-level failure:

    * ``"ok"``                - at least one finding received an explicit verdict
    * ``"challenger_failed"`` - the challenger task itself failed (completed=False)
    * ``"empty_logs"``        - the challenger posted no output
    * ``"no_json_array"``     - logs contain no JSON array with the expected anchor keys
    * ``"invalid_json"``      - a JSON array was found but could not be parsed
    * ``"no_hash_matches"``   - array parsed but no known finding hash matched

    FAIL-CLOSED: any finding with no explicit verdict defaults to
    passed=False/explicit=False. ``unresolved_reason`` on those entries carries
    the per-finding classification so operators can distinguish the failure mode
    without reading raw logs.
    """

    def _default_entry(unresolved_reason: str) -> dict:
        return {
            "passed": False,
            "reason": "fail-closed: no verdict returned for this finding",
            "explicit": False,
            "unresolved_reason": unresolved_reason,
        }

    if not completed:
        return {
            h: _default_entry(ADVERSARIAL_PARSE_CHALLENGER_FAILED)
            for h in finding_hashes
        }, ADVERSARIAL_PARSE_CHALLENGER_FAILED
    if not logs or not logs.strip():
        return {
            h: _default_entry(ADVERSARIAL_PARSE_EMPTY_LOGS) for h in finding_hashes
        }, ADVERSARIAL_PARSE_EMPTY_LOGS

    text = logs.strip()
    # Try extraction with different required-key combinations to support:
    # - hash+verdict (original: finding identified by hash)
    # - index+verdict (new: finding identified by 1-based ordinal)
    # - verdict alone (fallback: position-only binding)
    json_str = _extract_last_json_array(text, "hash", "verdict")
    if not json_str:
        json_str = _extract_last_json_array(text, "index", "verdict")
    if not json_str:
        json_str = _extract_last_json_array(text, "verdict")
    if not json_str:
        return {
            h: _default_entry(ADVERSARIAL_PARSE_NO_JSON_ARRAY) for h in finding_hashes
        }, ADVERSARIAL_PARSE_NO_JSON_ARRAY

    try:
        parsed = json.loads(json_str)
        if not isinstance(parsed, list):
            raise ValueError("not a list")
    except (json.JSONDecodeError, ValueError):
        return {
            h: _default_entry(ADVERSARIAL_PARSE_INVALID_JSON) for h in finding_hashes
        }, ADVERSARIAL_PARSE_INVALID_JSON

    result: dict[str, dict] = {
        h: _default_entry(ADVERSARIAL_PARSE_NO_HASH_MATCHES) for h in finding_hashes
    }
    matched_any = False

    # Pass 1: exact hash matching (primary binding).
    hash_matched_positions: set[int] = set()
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        h = str(item.get("hash") or "").strip()
        if h not in result:
            continue
        v = str(item.get("verdict") or "").strip().lower()
        reason = str(item.get("reason") or "")[:480]
        if v in ("pass", "passed", "approve", "approved"):
            result[h] = {"passed": True, "reason": reason, "explicit": True}
            matched_any = True
            hash_matched_positions.add(i)
        elif v in ("fail", "failed", "reject", "rejected"):
            result[h] = {"passed": False, "reason": reason, "explicit": True}
            matched_any = True
            hash_matched_positions.add(i)
        else:
            result[h]["unresolved_reason"] = "verdict_unrecognized"
            matched_any = True
            hash_matched_positions.add(i)

    # Pass 2 & 3: ordinal fallback for verdicts that did not match by hash.
    # Bind each unmatched verdict to the finding at the same ordinal position
    # (1-based index field takes priority over array position as a tiebreaker
    # when the challenger echoes the manifest's index field back).
    # Only binds to findings that still carry no explicit verdict (fail-closed
    # defaults from Pass 1 remain bindable until their position is claimed).
    unbound_positions: dict[int, str] = {
        i: h
        for i, h in enumerate(finding_hashes)
        if not result[h].get("explicit")
        and result[h].get("unresolved_reason") == ADVERSARIAL_PARSE_NO_HASH_MATCHES
    }
    claimed_positions: set[int] = set()

    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        if i in hash_matched_positions:
            continue  # already bound by exact hash
        # Determine which finding position to bind to.
        # Priority: explicit `index` field (1-based) > array position.
        target_pos: int | None = None
        idx_val = item.get("index")
        if idx_val is not None:
            try:
                explicit_pos = int(idx_val) - 1  # convert 1-based → 0-based
                if explicit_pos >= 0:
                    target_pos = explicit_pos
            except (ValueError, TypeError):
                pass
        if target_pos is None:
            target_pos = i
        if target_pos not in unbound_positions or target_pos in claimed_positions:
            continue
        target_hash = unbound_positions[target_pos]
        v = str(item.get("verdict") or "").strip().lower()
        reason = str(item.get("reason") or "")[:480]
        if v in ("pass", "passed", "approve", "approved"):
            result[target_hash] = {"passed": True, "reason": reason, "explicit": True}
            matched_any = True
        elif v in ("fail", "failed", "reject", "rejected"):
            result[target_hash] = {"passed": False, "reason": reason, "explicit": True}
            matched_any = True
        else:
            result[target_hash]["unresolved_reason"] = "verdict_unrecognized"
            matched_any = True
        claimed_positions.add(target_pos)

    if not matched_any:
        return result, ADVERSARIAL_PARSE_NO_HASH_MATCHES
    return result, ADVERSARIAL_PARSE_OK


def build_forward_quality_grader_prompt(
    task_type: str, artifact_text: str, *, question: str | None = None
) -> str:
    """Build the rubric prompt for an independent grader of ``artifact_text``.

    The grader runs as a fresh, read-only agent with no stake in the artifact.
    It must end its output with a single JSON verdict object so the verdict
    parser can read it back deterministically.

    When ``question`` is supplied (the task's declared topic/prompt) the rubric
    grows a RELEVANCE lens — without the question the grader sees only the
    artifact text and structurally cannot judge on-topic-ness.
    """
    label = task_type or "artifact"
    asked = (question or "").strip()
    relevance_block = "\n"
    if asked:
        relevance_block = (
            "  5. RELEVANCE — does the artifact actually answer what was asked "
            "below, rather than being well-formed but off-topic?\n\n"
            "Requested topic / prompt:\n"
            "-----8<----- REQUEST START -----8<-----\n"
            f"{asked[:4000]}\n"
            "-----8<----- REQUEST END -----8<-----\n\n"
        )
    return (
        "You are an INDEPENDENT quality grader. A prior agent produced the "
        f"following {label} artifact, and its findings are about to SPAWN "
        "downstream implementation work (PRs). Your job is to decide, "
        "independently, whether that is safe — exactly like a code reviewer "
        "gates a merge, but here you are gating whether findings get to spawn "
        "work.\n\n"
        "Grade the artifact against this rubric:\n"
        "  1. GROUNDEDNESS — are the claims supported by real, cited evidence "
        "(files, PRs, messages) rather than speculation?\n"
        "  2. CORRECTNESS — do the claims hold up against the actual codebase "
        "at HEAD? Spot-check the most load-bearing ones.\n"
        "  3. ACTIONABILITY — is each spawnable finding specific and scoped "
        "enough that an implementer could act on it without guessing?\n"
        "  4. COHERENCE — is the artifact internally consistent and parseable?\n"
        f"{relevance_block}"
        "Be skeptical. If the artifact is thin, ungrounded, internally "
        "contradictory, off-topic, or its load-bearing claims do not survive a "
        "spot-check, FAIL it — a failing grade prevents unverified findings "
        "from spawning work, which is the safe default.\n\n"
        "Artifact under review:\n"
        "-----8<----- ARTIFACT START -----8<-----\n"
        f"{artifact_text[:20000]}\n"
        "-----8<----- ARTIFACT END -----8<-----\n\n"
        "End your response with EXACTLY ONE JSON object on its own line, and "
        "nothing after it:\n"
        '{"verdict": "pass" | "fail", "reasoning": "<one-sentence justification>"}'
    )


async def spawn_forward_quality_grader(
    source_task: Task,
    artifact_text: str,
    session: AsyncSession,
    *,
    rubric: str | None = None,
    question: str | None = None,
    adversarial_mode: bool = False,
    finding_hashes: list[str] | None = None,
) -> Task | None:
    """Spawn an independent grader task for ``source_task`` and park the source.

    Returns the spawned grader ``Task``, or ``None`` when grading is already in
    flight or already resolved (single-shot guard — a source artifact is graded
    at most once; this is the convergence control, analogous to the review
    pipeline's per-chain iteration cap).

    When ``adversarial_mode=True``, the grader uses a refute-first per-finding
    challenge prompt and writes ``ADVERSARIAL_GATE_VERDICT_KEY`` on the source
    rather than ``QUALITY_GRADER_VERDICT_KEY``. The two gates are independent
    and use separate awaiting/verdict keys so they can both run on a single
    research task without interfering.

    The source task is moved to EVALUATING and will NOT complete or spawn
    children until the grader's verdict returns via the re-entrant completion
    path (``task_lifecycle._resolve_quality_grader_completion``).
    """
    ctx = dict(source_task.context or {})
    if adversarial_mode:
        if ctx.get(ADVERSARIAL_GATE_VERDICT_KEY) is not None or ctx.get(
            "awaiting_adversarial_grader"
        ):
            return None
    else:
        if ctx.get(QUALITY_GRADER_VERDICT_KEY) is not None or ctx.get(
            "awaiting_quality_grader"
        ):
            # Already graded, or a grader is already in flight — never double-spawn.
            return None

    task_type = _task_type_of(source_task)
    if adversarial_mode:
        prompt = rubric or build_adversarial_finding_prompt(artifact_text)
        _awaiting_key = "awaiting_adversarial_grader"
        _title_suffix = "adversarial-challenge"
        _description = (
            "Independent adversarial challenge of high-stakes research findings "
            "before they are allowed to spawn downstream implementation work."
        )
        _status_message = (
            "Awaiting adversarial challenge verdict before spawning "
            "high-stakes implementation tasks"
        )
    else:
        prompt = rubric or build_forward_quality_grader_prompt(
            task_type, artifact_text, question=question
        )
        _awaiting_key = "awaiting_quality_grader"
        _title_suffix = "forward-quality grade"
        _description = (
            "Independent forward-quality grade of an upstream artifact before "
            "it is allowed to spawn downstream implementation work."
        )
        _status_message = (
            "Awaiting independent forward-quality grade before spawning "
            "implementation tasks"
        )
    base_branch = (source_task.first_payload or {}).get("base_branch", "main")

    grader = Task(
        project_id=source_task.project_id,
        title=f"[GRADE] {_title_suffix} for {task_type} {str(source_task.id)[:8]}",
        description=_description,
        # Deliberately NOT parent_task_id=source: the grader is a side-car, not
        # a rollup child. Linking it as a child would entangle it in the
        # source's parent-rollup accounting. The back-reference lives in
        # context (QUALITY_GRADER_SOURCE_KEY) instead.
        parent_task_id=None,
        payloads=[
            {
                "type": "investigate",
                "prompt": prompt,
                "base_branch": base_branch,
                "scope": "small",
                # Deliberate cost-efficiency tier: mid-pipeline second opinion,
                # not the final merge gate (spawn_review_second_opinion = Opus).
                # Accepted finding; pinned by test_spawn_forward_quality_grader_sets_sonnet_quality_tier.
                "quality_tier": "sonnet",
            }
        ],
        context={
            QUALITY_GRADER_SOURCE_KEY: str(source_task.id),
            "scope": "small",
            "graded_task_type": task_type,
            **({"adversarial_grader": True} if adversarial_mode else {}),
            **(
                {"_adversarial_challenged_hashes": finding_hashes}
                if adversarial_mode and finding_hashes
                else {}
            ),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        priority=source_task.priority,
    )
    session.add(grader)

    # Park the source awaiting the verdict. Overrides the COMPLETED status the
    # orchestrator set pre-handler — the source is NOT done until graded.
    source_task.status = TaskStatus.EVALUATING
    source_task.latest_status_message = _status_message[:500]
    ctx[_awaiting_key] = True
    source_task.context = ctx
    flag_modified(source_task, "context")
    session.add(source_task)
    await session.commit()
    await session.refresh(grader)

    # Record the concrete grader id for observability now that it exists.
    ctx2 = dict(source_task.context or {})
    ctx2[_awaiting_key] = str(grader.id)
    source_task.context = ctx2
    flag_modified(source_task, "context")
    session.add(source_task)
    await session.commit()

    logger.info(
        "[EVAL] Spawned %s grader %s for %s task %s; source parked "
        "EVALUATING awaiting verdict",
        "adversarial" if adversarial_mode else "forward-quality",
        grader.id,
        task_type,
        source_task.id,
    )
    return grader


# (safeguard_challenge lane removed — observe-only, no measured lift)


def parse_grader_verdict(logs: str | None, completed: bool = True) -> EvaluationVerdict:
    """Parse an independent grader's pass/fail verdict from its output.

    FAIL-CLOSED on every ambiguous case: a grader that did not complete, emitted
    nothing, or produced output we cannot parse yields a FAILED verdict, so
    ungraded findings never spawn downstream work.
    """
    if not completed:
        return EvaluationVerdict(
            False, "grader task did not complete; findings ungraded", "forward-quality"
        )
    text = (logs or "").strip()
    if not text:
        return EvaluationVerdict(False, "grader produced no output", "forward-quality")

    obj = scan_balanced_json_objects(text, required_key="verdict")
    if obj is not None:
        v = str(obj.get("verdict", "")).strip().lower()
        reason = str(obj.get("reasoning") or obj.get("reason") or "")[:480]
        if v in ("pass", "passed", "approve", "approved", "accept"):
            return EvaluationVerdict(
                True, reason or "grader passed the artifact", "forward-quality"
            )
        if v in ("fail", "failed", "reject", "rejected", "request_changes"):
            return EvaluationVerdict(
                False, reason or "grader failed the artifact", "forward-quality"
            )

    # Fallback: anchored keyword scan, last occurrence wins.
    low = text.lower()
    pass_idx = max(
        low.rfind('"verdict": "pass"'),
        low.rfind('"verdict":"pass"'),
        low.rfind("verdict: pass"),
    )
    fail_idx = max(
        low.rfind('"verdict": "fail"'),
        low.rfind('"verdict":"fail"'),
        low.rfind("verdict: fail"),
    )
    if fail_idx > pass_idx:
        return EvaluationVerdict(
            False, "grader verdict: fail (anchored)", "forward-quality"
        )
    if pass_idx >= 0:
        return EvaluationVerdict(
            True, "grader verdict: pass (anchored)", "forward-quality"
        )
    return EvaluationVerdict(
        False, "grader verdict unparseable; failing closed", "forward-quality"
    )


# ---------------------------------------------------------------------------
# Per-finding second-opinion CHALLENGE implementation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingChallengeRecord:
    """Structured record of an independent second opinion on ONE finding verdict.

    ``challenged_boundary`` is the high-stakes verdict being re-examined (one of
    the ``CHALLENGE_BOUNDARY_*`` constants). ``first_verdict`` is the original
    single-pass classification being challenged (e.g. ``"absent"`` / ``"LOW"``).
    ``second_opinion_verdict`` is the independent re-classification the side-car
    produced. ``agreement`` is one of ``agrees`` / ``disagrees`` /
    ``indeterminate`` — and is ``indeterminate`` whenever the side-car's output
    cannot be parsed or the side-car did not complete (fail-closed, so an
    ambiguous second opinion never overturns the original verdict). ``reason`` is
    a human-readable justification; ``sidecar_task_id`` is the challenge task's
    id for observability and audit.

    ``schema_version`` is the record schema version (always
    ``FINDING_CHALLENGE_RECORD_SCHEMA_VERSION`` for newly created records; absent
    on legacy records pre-dating versioning). ``source_task_id`` is the id of the
    research task whose finding was challenged; ``produced_by`` identifies the
    pipeline stage that created this record (always ``"challenge_sidecar"`` for
    records created through the normal challenge path).
    """

    challenged_boundary: str
    first_verdict: str
    second_opinion_verdict: str
    agreement: str
    reason: str
    sidecar_task_id: str | None
    schema_version: int = FINDING_CHALLENGE_RECORD_SCHEMA_VERSION
    source_task_id: str | None = None
    produced_by: str = "challenge_sidecar"
    finding_hash: str | None = None

    def to_context(self) -> dict[str, Any]:
        """Plain-dict form for stamping under ``FINDING_CHALLENGE_VERDICT_KEY``."""
        return {
            "challenged_boundary": self.challenged_boundary,
            "first_verdict": self.first_verdict,
            "second_opinion_verdict": self.second_opinion_verdict,
            "agreement": self.agreement,
            "reason": self.reason,
            "sidecar_task_id": self.sidecar_task_id,
            "schema_version": self.schema_version,
            "source_task_id": self.source_task_id,
            "produced_by": self.produced_by,
            "finding_hash": self.finding_hash,
        }


def build_finding_challenge_prompt(
    boundary: str,
    finding: dict[str, Any],
    first_verdict: str,
    *,
    question: str | None = None,
) -> str:
    """Build a read-only rubric prompt challenging ONE finding's verdict.

    The challenger runs as a fresh, read-only agent with no stake in the
    original verdict. It re-examines a SINGLE finding (not the whole artifact)
    against ``first_verdict`` and must end its output with exactly one JSON
    object so ``parse_finding_challenge_verdict`` can read it back
    deterministically (reusing the grader's end-with-one-JSON convention).

    ``boundary`` selects the framing: ``reproduce_absent`` asks whether the cited
    code is truly gone vs. merely moved/renamed; ``low_actionability_decay`` asks
    whether the finding is truly too vague to act on. When ``question`` (the
    research task's topic) is supplied the challenger is told what was originally
    asked so it can judge on-topic-ness.
    """
    if boundary == CHALLENGE_BOUNDARY_REPRODUCE_ABSENT:
        framing = (
            "The first pass judged this finding's cited code ABSENT on HEAD and "
            "is about to HARD-DROP it (the finding would be rejected forever). "
            "But an 'absent' verdict is derived purely from whether cited file "
            "PATHS and SYMBOL references still resolve at HEAD — code that was "
            "MOVED or RENAMED rather than deleted reads as 'absent' too. "
            "Independently decide: is the cited code TRULY gone, or merely "
            "relocated/renamed (in which case the finding should NOT be dropped)?"
        )
    elif boundary == CHALLENGE_BOUNDARY_LOW_ACTIONABILITY_DECAY:
        framing = (
            "The first pass decayed this finding to LOW actionability and is "
            "about to drop it as too vague to spawn work. Independently decide: "
            "is the finding genuinely unactionable, or is it specific and scoped "
            "enough that an implementer could act on it (in which case the decay "
            "is wrong and the finding should be dispatched)?"
        )
    elif boundary == CHALLENGE_BOUNDARY_REPRODUCE_INDETERMINATE_DISPATCH:
        framing = (
            "The first pass could not determine whether this finding reproduces "
            "on HEAD and is about to DISPATCH it unverified. Independently "
            "decide: does the finding actually reproduce against the codebase at "
            "HEAD, or is it stale/already-fixed (in which case it should NOT be "
            "dispatched)?"
        )
    else:
        framing = (
            "Independently re-examine the first-pass verdict on this finding and "
            "decide whether it holds up."
        )

    asked = (question or "").strip()
    relevance_block = ""
    if asked:
        relevance_block = (
            "\nFor context, the research task was asked:\n"
            "-----8<----- REQUEST START -----8<-----\n"
            f"{asked[:2000]}\n"
            "-----8<----- REQUEST END -----8<-----\n"
        )

    try:
        finding_json = json.dumps(finding, indent=2, default=str)[:8000]
    except (TypeError, ValueError):
        finding_json = str(finding)[:8000]

    return (
        "You are an INDEPENDENT second opinion on a SINGLE research finding. A "
        "prior agent reached a high-stakes verdict on this finding, and that "
        "verdict is about to be final. Your job is to challenge it — read-only, "
        "with no stake in the original call.\n\n"
        f"{framing}\n"
        f"\nThe first-pass verdict being challenged is: {first_verdict!r}.\n"
        f"{relevance_block}"
        "\nFinding under challenge:\n"
        "-----8<----- FINDING START -----8<-----\n"
        f"{finding_json}\n"
        "-----8<----- FINDING END -----8<-----\n\n"
        "Investigate the actual codebase at HEAD as needed. Be specific about "
        "what you checked. If you cannot reach a confident conclusion, say so "
        "(verdict 'indeterminate') rather than guessing — an indeterminate "
        "second opinion preserves the original verdict.\n\n"
        "End your response with EXACTLY ONE JSON object on its own line, and "
        "nothing after it:\n"
        '{"verdict": "<your independent classification>", '
        '"agreement": "agrees" | "disagrees" | "indeterminate", '
        '"reasoning": "<one-sentence justification>"}'
    )


async def spawn_finding_challenge(
    source_task: Task,
    finding: dict[str, Any],
    boundary: str,
    first_verdict: str,
    session: AsyncSession,
    *,
    question: str | None = None,
    finding_hash: str | None = None,
) -> Task | None:
    """Spawn a single read-only challenge side-car and park the source EVALUATING.

    Returns the spawned challenge ``Task``, or ``None`` when a challenge is
    already in flight or already resolved (single-shot guard — a finding verdict
    is challenged at most once). The source is moved to EVALUATING and will NOT
    complete or spawn children until the second opinion returns via the
    re-entrant completion path
    (``task_lifecycle._resolve_finding_challenge_completion``).

    An unrecognized ``boundary`` is rejected (returns ``None``) so a caller can
    never park a source on a challenge framing the prompt builder cannot express.
    """
    if boundary not in _CHALLENGE_BOUNDARIES:
        logger.warning(
            "[CHALLENGE] Unknown challenge boundary %r for task %s — not spawning",
            boundary,
            source_task.id,
        )
        return None

    ctx = dict(source_task.context or {})
    if ctx.get(FINDING_CHALLENGE_VERDICT_KEY) is not None or ctx.get(
        "awaiting_finding_challenge"
    ):
        # Already challenged, or a challenge is already in flight — never
        # double-spawn (mirrors spawn_forward_quality_grader's single-shot guard).
        return None

    prompt = build_finding_challenge_prompt(
        boundary, finding, first_verdict, question=question
    )
    base_branch = (source_task.first_payload or {}).get("base_branch", "main")

    challenge = Task(
        project_id=source_task.project_id,
        title=(
            f"[CHALLENGE] {boundary} second opinion for research "
            f"{str(source_task.id)[:8]}"
        ),
        description=(
            "Independent per-finding second opinion on a high-stakes research "
            "verdict before the finding is dropped or dispatched."
        ),
        # Deliberately NOT parent_task_id=source: the challenge is a side-car, not
        # a rollup child. The back-reference lives in context instead.
        parent_task_id=None,
        payloads=[
            {
                "type": "investigate",
                "prompt": prompt,
                "base_branch": base_branch,
                "scope": "small",
                # Deliberate cost-efficiency tier: mid-pipeline second opinion,
                # not the final merge gate. Accepted finding; pinned by
                # test_spawn_finding_challenge_sets_sonnet_quality_tier.
                "quality_tier": "sonnet",
            }
        ],
        context={
            FINDING_CHALLENGE_SOURCE_KEY: str(source_task.id),
            "scope": "small",
            "challenged_boundary": boundary,
            "challenged_first_verdict": first_verdict,
            **({"challenged_finding_hash": finding_hash} if finding_hash else {}),
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        priority=source_task.priority,
    )
    session.add(challenge)

    # Park the source awaiting the second opinion. Overrides the COMPLETED status
    # the orchestrator set pre-handler — the source is NOT done until challenged.
    source_task.status = TaskStatus.EVALUATING
    source_task.latest_status_message = (
        f"Awaiting independent second opinion on a {boundary} verdict before "
        "dropping/dispatching the finding"
    )[:500]
    ctx["awaiting_finding_challenge"] = True
    source_task.context = ctx
    flag_modified(source_task, "context")
    session.add(source_task)
    await session.commit()
    await session.refresh(challenge)

    # Record the concrete side-car id for observability now that it exists.
    ctx2 = dict(source_task.context or {})
    ctx2["awaiting_finding_challenge"] = str(challenge.id)
    source_task.context = ctx2
    flag_modified(source_task, "context")
    session.add(source_task)
    await session.commit()

    logger.info(
        "[CHALLENGE] Spawned %s challenge %s for research task %s; source parked "
        "EVALUATING awaiting second opinion",
        boundary,
        challenge.id,
        source_task.id,
    )
    return challenge


def parse_finding_challenge_verdict(
    logs: str | None,
    first_verdict: str,
    boundary: str,
    sidecar_task_id: str | None,
    completed: bool = True,
    *,
    source_task_id: str | None = None,
    finding_hash: str | None = None,
) -> FindingChallengeRecord:
    """Parse a challenge side-car's second opinion into a FindingChallengeRecord.

    FAIL-CLOSED on every ambiguous case: a side-car that did not complete,
    emitted nothing, or produced output we cannot parse yields
    ``agreement="indeterminate"`` with a recorded reason, so an unparseable or
    incomplete second opinion never overturns the original verdict. A parsed
    ``agreement`` outside the recognized set also collapses to indeterminate.

    ``source_task_id`` is the id of the research task whose finding was
    challenged; it is stamped onto the returned record for provenance.
    """
    if not completed:
        return FindingChallengeRecord(
            challenged_boundary=boundary,
            first_verdict=first_verdict,
            second_opinion_verdict="",
            agreement=CHALLENGE_INDETERMINATE,
            reason="challenge side-car did not complete; verdict unchallenged",
            sidecar_task_id=sidecar_task_id,
            source_task_id=source_task_id,
            finding_hash=finding_hash,
        )

    text = (logs or "").strip()
    if not text:
        return FindingChallengeRecord(
            challenged_boundary=boundary,
            first_verdict=first_verdict,
            second_opinion_verdict="",
            agreement=CHALLENGE_INDETERMINATE,
            reason="challenge side-car produced no output",
            sidecar_task_id=sidecar_task_id,
            source_task_id=source_task_id,
            finding_hash=finding_hash,
        )

    obj = scan_balanced_json_objects(text, required_key="verdict", last=True)
    if obj is None:
        return FindingChallengeRecord(
            challenged_boundary=boundary,
            first_verdict=first_verdict,
            second_opinion_verdict="",
            agreement=CHALLENGE_INDETERMINATE,
            reason="challenge verdict unparseable; failing closed",
            sidecar_task_id=sidecar_task_id,
            source_task_id=source_task_id,
            finding_hash=finding_hash,
        )

    second = str(obj.get("verdict") or "").strip()
    reason = str(obj.get("reasoning") or obj.get("reason") or "")[:480]
    agreement = str(obj.get("agreement") or "").strip().lower()
    if agreement not in (
        CHALLENGE_AGREES,
        CHALLENGE_DISAGREES,
        CHALLENGE_INDETERMINATE,
    ):
        agreement = CHALLENGE_INDETERMINATE
        if not reason:
            reason = "challenge omitted a recognized agreement; failing closed"
    return FindingChallengeRecord(
        challenged_boundary=boundary,
        first_verdict=first_verdict,
        second_opinion_verdict=second,
        agreement=agreement,
        reason=reason or f"second opinion {agreement} with the {boundary} verdict",
        sidecar_task_id=sidecar_task_id,
        source_task_id=source_task_id,
        finding_hash=finding_hash,
    )

