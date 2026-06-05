"""Pluggable per-type evaluation gate framework.

Code task types (create/fix/refactor/qa/migrate/deps/docs) get an honest
"was this output any good" answer from the existing two-flag merge gate — a
PR cannot merge until CI is green AND a review signs off. Non-code task types
historically had NO forward quality gate: a memo with unparseable output, or a
perf report that measured nothing, was parsed, stored, and marked COMPLETED on
the agent's say-so.

This module provides the FOUNDATION of a per-type forward gate. A small closed
set of strategies (``none``, ``forward-quality``, ``metric``,
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
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import Task, TaskStatus
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
            from app.task_completion import _parse_memo_output  # noqa: PLC0415

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
    """

    key = "metric"

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        if _task_type_of(task) == "compute_experiment":
            return self._evaluate_compute_experiment(artifact_text)
        measurement = _extract_measurement(artifact_text)
        if measurement is None:
            return EvaluationVerdict(
                False,
                "metric task produced no parseable structured measurement",
                self.key,
            )
        return EvaluationVerdict(
            True,
            f"structured measurement produced ({len(measurement)} item(s))",
            self.key,
        )

    def _evaluate_compute_experiment(
        self, artifact_text: str | None
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
        return EvaluationVerdict(
            True,
            f"compute experiment reached terminal outcome {outcome!r} "
            f"at measured cost ${cost}",
            self.key,
        )


class StructuredVerdictStrategy:
    """Strict structural gate for multi-lens verdict artifacts."""

    key = "structured-verdict"

    def evaluate(self, task: Task, artifact_text: str | None) -> EvaluationVerdict:
        text = (artifact_text or "").strip()
        if not text:
            return EvaluationVerdict(
                False, "empty terminal artifact — no structured verdict produced", self.key
            )

        from app.task_completion import _parse_panel_review_output  # noqa: PLC0415

        parsed = _parse_panel_review_output(artifact_text)
        if parsed is None:
            return EvaluationVerdict(
                False,
                "structured verdict output is unparseable or missing required fields",
                self.key,
            )
        return EvaluationVerdict(
            True, "structured verdict parsed with all declared lenses", self.key
        )


# Registry of concrete strategy instances, keyed by their declared strategy key.
# Strategies are stateless, so module-level singletons are safe to share.
_STRATEGIES: dict[str, EvaluationStrategy] = {
    s.key: s
    for s in (
        NoneStrategy(),
        CodeCiReviewStrategy(),
        ForwardQualityStrategy(),
        MetricStrategy(),
        StructuredVerdictStrategy(),
    )
}


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


def evaluate_terminal_output(task: Task, artifact_text: str | None) -> EvaluationVerdict:
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
    from app.task_completion import _extract_json_array  # noqa: PLC0415

    json_str = _extract_json_array(text, "severity")
    if not json_str:
        return None
    try:
        parsed = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _coerce_number(value: Any) -> float | None:
    """Return ``value`` as a float when it is a real number, else ``None``.

    ``bool`` is a subclass of ``int`` in Python, so it is explicitly rejected —
    ``True``/``False`` is never a valid cost measurement.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


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
    obj = _extract_json_object(artifact_text)
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


def _extract_json_object(artifact_text: str | None) -> dict | None:
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
    obj = _extract_verdict_object_any(text)
    return obj


def _extract_verdict_object_any(text: str) -> dict | None:
    """Find the first balanced JSON object in ``text`` that parses to a dict."""
    idx = text.find("{")
    while idx != -1:
        depth = 0
        for j in range(idx, len(text)):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[idx : j + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        idx = text.find("{", idx + 1)
    return None


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
) -> Task | None:
    """Spawn an independent grader task for ``source_task`` and park the source.

    Returns the spawned grader ``Task``, or ``None`` when grading is already in
    flight or already resolved (single-shot guard — a source artifact is graded
    at most once; this is the convergence control, analogous to the review
    pipeline's per-chain iteration cap).

    The source task is moved to EVALUATING and will NOT complete or spawn
    children until the grader's verdict returns via the re-entrant completion
    path (``task_lifecycle._resolve_quality_grader_completion``).
    """
    ctx = dict(source_task.context or {})
    if ctx.get(QUALITY_GRADER_VERDICT_KEY) is not None or ctx.get(
        "awaiting_quality_grader"
    ):
        # Already graded, or a grader is already in flight — never double-spawn.
        return None

    task_type = _task_type_of(source_task)
    prompt = rubric or build_forward_quality_grader_prompt(
        task_type, artifact_text, question=question
    )
    base_branch = (source_task.first_payload or {}).get("base_branch", "main")

    grader = Task(
        project_id=source_task.project_id,
        title=f"[GRADE] forward-quality grade for {task_type} {str(source_task.id)[:8]}",
        description=(
            "Independent forward-quality grade of an upstream artifact before "
            "it is allowed to spawn downstream implementation work."
        ),
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
            }
        ],
        context={
            QUALITY_GRADER_SOURCE_KEY: str(source_task.id),
            "scope": "small",
            "graded_task_type": task_type,
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
    source_task.latest_status_message = (
        "Awaiting independent forward-quality grade before spawning "
        "implementation tasks"
    )[:500]
    ctx["awaiting_quality_grader"] = True
    source_task.context = ctx
    flag_modified(source_task, "context")
    session.add(source_task)
    await session.commit()
    await session.refresh(grader)

    # Record the concrete grader id for observability now that it exists.
    ctx2 = dict(source_task.context or {})
    ctx2["awaiting_quality_grader"] = str(grader.id)
    source_task.context = ctx2
    flag_modified(source_task, "context")
    session.add(source_task)
    await session.commit()

    logger.info(
        "[EVAL] Spawned forward-quality grader %s for %s task %s; source parked "
        "EVALUATING awaiting verdict",
        grader.id,
        task_type,
        source_task.id,
    )
    return grader


def _extract_verdict_object(text: str) -> dict | None:
    """Find the first balanced JSON object containing a ``verdict`` key."""
    idx = text.find("{")
    while idx != -1:
        depth = 0
        for j in range(idx, len(text)):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[idx : j + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and "verdict" in obj:
                            return obj
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        idx = text.find("{", idx + 1)
    return None


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

    obj = _extract_verdict_object(text)
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
